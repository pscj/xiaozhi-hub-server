"""信息屏新闻接口 GET /news.json：不用登录，缓存优先、回源失败回退旧缓存、无缓存时 503。

回源是 AIHOT 的 RSS，网关侧转换成 {date, items}；转换规则（24 小时窗口、分类映射、
教程/观点过滤、上限 8 条）在 convert_aihot_feed / map_news_category 里单测，
回源请求一律 monkeypatch 掉，不真打外网。
"""

import email.utils
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.admin.admin_handler import AdminHandler, convert_aihot_feed, map_news_category

NEWS = {"date": "2026-11-12", "items": [{"category": "模型", "title": "标题", "summary": ""}]}

NOW = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc)  # 上海时间 2026-11-13 09:00


def _rss(items) -> bytes:
    """把 (标题, 分类, 发布时间) 列表拼成 AIHOT 形态的 RSS；分类为 None 表示不带 category。"""
    parts = ['<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>AIHOT</title>']
    for title, category, published in items:
        parts.append("<item>")
        parts.append(f"<title>{title}</title>")
        if category is not None:
            parts.append(f"<category>{category}</category>")
        parts.append(f"<pubDate>{email.utils.format_datetime(published)}</pubDate>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


@pytest.fixture
def cfg_path(tmp_path) -> Path:
    p = tmp_path / ".config.yaml"
    p.write_text("selected_module: {}\n", encoding="utf-8")
    return p


@pytest.fixture
async def client(cfg_path):
    app = web.Application()
    AdminHandler({"selected_module": {}}, ws_server=None, config_path=cfg_path).register(app)
    async with TestClient(TestServer(app)) as c:
        yield c


@pytest.fixture
def cache_file(cfg_path) -> Path:
    # 缓存和配置文件放同一目录（data_dir 取 config_path 的父目录）
    return cfg_path.parent / "news.json"


async def test_news_json_503_without_cache(client, monkeypatch):
    monkeypatch.setattr(AdminHandler, "_fetch_news_upstream", lambda self: None)
    r = await client.get("/news.json")
    assert r.status == 503
    assert await r.json() == {"ok": False, "error": "news not available yet"}


async def test_news_json_serves_fresh_cache_without_fetch(client, cache_file, monkeypatch):
    cache_file.write_text(json.dumps(NEWS, ensure_ascii=False), encoding="utf-8")

    def fail_fetch(self):
        raise AssertionError("缓存新鲜时不应回源")

    monkeypatch.setattr(AdminHandler, "_fetch_news_upstream", fail_fetch)
    r = await client.get("/news.json")
    assert r.status == 200
    assert await r.json() == NEWS


async def test_news_json_falls_back_to_stale_cache(client, cache_file, monkeypatch):
    cache_file.write_text(json.dumps(NEWS, ensure_ascii=False), encoding="utf-8")
    old = time.time() - 3600  # 过期一小时，必须回源；回源失败则回退这份旧缓存
    os.utime(cache_file, (old, old))
    monkeypatch.setattr(AdminHandler, "_fetch_news_upstream", lambda self: None)
    r = await client.get("/news.json")
    assert r.status == 200
    assert await r.json() == NEWS


async def test_news_json_returns_fresh_fetch(client, monkeypatch):
    monkeypatch.setattr(AdminHandler, "_fetch_news_upstream", lambda self: NEWS)
    r = await client.get("/news.json")
    assert r.status == 200
    assert await r.json() == NEWS


# ---------- 分类映射 ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AI 模型", "模型"),
        ("AI 产品", "产品"),
        ("行业动态", "行业"),
        ("论文", "模型"),
        ("未见过的新分类", "行业"),
        ("", "行业"),
        (None, "行业"),
        (" AI 产品 ", "产品"),  # 带空白也能认
        ("教程", None),
        ("观点", None),
    ],
)
def test_map_news_category(raw, expected):
    assert map_news_category(raw) == expected


# ---------- RSS 转换 ----------


def test_convert_filters_window_and_sorts_desc():
    xml = _rss(
        [
            ("两小时前", "行业动态", NOW - timedelta(hours=2)),
            ("刚刚", "行业动态", NOW - timedelta(minutes=5)),
            ("25 小时前", "行业动态", NOW - timedelta(hours=25)),
            ("23 小时前", "行业动态", NOW - timedelta(hours=23)),
        ]
    )
    data = convert_aihot_feed(xml, NOW)
    assert [i["title"] for i in data["items"]] == ["刚刚", "两小时前", "23 小时前"]
    assert data["date"] == "2026-11-13"
    assert all(i["summary"] == "" for i in data["items"])


def test_convert_caps_at_8_items():
    xml = _rss([(f"第{n}条", "AI 产品", NOW - timedelta(minutes=n)) for n in range(10)])
    data = convert_aihot_feed(xml, NOW)
    assert len(data["items"]) == 8
    assert data["items"][0]["title"] == "第0条"  # 最新的在最前


def test_convert_maps_categories_and_skips_tutorial_and_opinion():
    xml = _rss(
        [
            ("模型新闻", "AI 模型", NOW - timedelta(hours=1)),
            ("论文新闻", "论文", NOW - timedelta(hours=1)),
            ("新分类新闻", "某个新分类", NOW - timedelta(hours=1)),
            ("无分类新闻", None, NOW - timedelta(hours=1)),
            ("教程贴", "教程", NOW - timedelta(hours=1)),
            ("观点贴", "观点", NOW - timedelta(hours=1)),
        ]
    )
    data = convert_aihot_feed(xml, NOW)
    got = {i["title"]: i["category"] for i in data["items"]}
    assert got == {"模型新闻": "模型", "论文新闻": "模型", "新分类新闻": "行业", "无分类新闻": "行业"}


def test_convert_strips_title_whitespace():
    xml = _rss([("  带空白的标题 \n", "AI 模型", NOW - timedelta(hours=1))])
    data = convert_aihot_feed(xml, NOW)
    assert data["items"][0]["title"] == "带空白的标题"


def test_convert_date_is_shanghai_today():
    # UTC 2026-11-12 16:30 已是上海的 2026-11-13 00:30
    now = datetime(2026, 11, 12, 16, 30, tzinfo=timezone.utc)
    xml = _rss([("半小时前", "AI 模型", now - timedelta(minutes=30))])
    assert convert_aihot_feed(xml, now)["date"] == "2026-11-13"


def test_convert_returns_none_when_nothing_usable():
    assert convert_aihot_feed(b"this is not xml", NOW) is None
    old_only = _rss([("三天前", "AI 模型", NOW - timedelta(days=3))])
    assert convert_aihot_feed(old_only, NOW) is None
    skipped_only = _rss([("教程贴", "教程", NOW - timedelta(hours=1))])
    assert convert_aihot_feed(skipped_only, NOW) is None


# ---------- 真回源（只 stub httpx.get，不真打外网） ----------


class _FakeResp:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


async def test_news_json_fetches_rss_and_caches(client, cache_file, monkeypatch):
    now = datetime.now(timezone.utc)
    xml = _rss(
        [
            ("最新产品", "AI 产品", now - timedelta(minutes=10)),
            ("过期新闻", "AI 模型", now - timedelta(hours=30)),
        ]
    )
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResp(xml))
    r = await client.get("/news.json")
    assert r.status == 200
    body = await r.json()
    assert [i["title"] for i in body["items"]] == ["最新产品"]
    assert body["items"][0]["category"] == "产品"
    # 转换结果同时写进了缓存文件
    assert json.loads(cache_file.read_text(encoding="utf-8")) == body


async def test_news_json_fetch_failure_falls_back_to_stale_cache(client, cache_file, monkeypatch):
    cache_file.write_text(json.dumps(NEWS, ensure_ascii=False), encoding="utf-8")
    old = time.time() - 3600
    os.utime(cache_file, (old, old))

    def boom(*a, **kw):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(httpx, "get", boom)
    r = await client.get("/news.json")
    assert r.status == 200
    assert await r.json() == NEWS

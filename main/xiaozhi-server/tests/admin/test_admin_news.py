"""信息屏新闻接口 GET /news.json：不用登录，缓存优先、回源失败回退旧缓存、无缓存时 503。

回源是 AIHOT 的热点榜 API（hot-topics），网关侧转换成 {date, items}；2026-11-23 起
不筛选直用：按榜单原顺序输出、条数不截断、分类留空（客户端不显示标签），转换规则在
convert_hot_topics 里单测，回源请求一律 monkeypatch 掉，不真打外网。
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.admin.admin_handler import AdminHandler, convert_hot_topics

NEWS = {"date": "2026-11-23", "items": [{"category": "", "title": "标题", "summary": ""}]}

NOW = datetime(2026, 11, 23, 7, 0, tzinfo=timezone.utc)  # 上海时间 2026-11-23 15:00


def _hot_topics(entries) -> bytes:
    """把条目列表拼成 hot-topics 形态的 JSON；条目原样放进 items，可传 dict 或原始值。"""
    doc = {"schemaVersion": 1, "count": len(entries), "items": list(entries)}
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


def _entry(rank, title, latest_at="2026-11-23T01:00:23.127Z"):
    """一条贴近真实接口形状的热点条目。"""
    return {
        "rank": rank,
        "id": f"id{rank}",
        "storyPublicId": f"story{rank}",
        "title": title,
        "source": {"name": "TechCrunch：AI（RSS）"},
        "links": {"aihot": f"https://aihot.news/items/id{rank}"},
        "sourceCount": 7,
        "signalCount": 28,
        "participantCount": 35,
        "sourceNames": ["TechCrunch：AI（RSS）"],
        "latestAt": latest_at,
    }


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


# ---------- 热点榜转换 ----------


def test_convert_keeps_rank_order_even_if_latest_at_unordered():
    # latestAt 故意乱序（rank 1 的最新动态反而最早）：输出必须按榜单原顺序，不按时间重排
    payload = _hot_topics(
        [
            _entry(1, "第一名", latest_at="2026-11-22T01:00:00.000Z"),
            _entry(2, "第二名", latest_at="2026-11-23T06:00:00.000Z"),
            _entry(3, "第三名", latest_at="2026-11-21T01:00:00.000Z"),
        ]
    )
    data = convert_hot_topics(payload, NOW)
    assert [i["title"] for i in data["items"]] == ["第一名", "第二名", "第三名"]
    assert data["date"] == "2026-11-23"
    assert all(i["category"] == "" for i in data["items"])
    assert all(i["summary"] == "" for i in data["items"])


def test_convert_passes_all_10_items_without_cap():
    payload = _hot_topics([_entry(n, f"第{n}条") for n in range(1, 11)])
    data = convert_hot_topics(payload, NOW)
    assert len(data["items"]) == 10
    assert data["items"][0]["title"] == "第1条"
    assert data["items"][-1]["title"] == "第10条"


def test_convert_skips_empty_or_missing_titles():
    payload = _hot_topics(
        [
            _entry(1, "正常标题"),
            _entry(2, ""),
            _entry(3, "   "),
            {"rank": 4, "id": "id4"},  # 没有 title 字段
            _entry(5, None),
            _entry(6, "  带空白的标题 \n"),
            "不是对象的条目",
        ]
    )
    data = convert_hot_topics(payload, NOW)
    assert [i["title"] for i in data["items"]] == ["正常标题", "带空白的标题"]


def test_convert_date_is_shanghai_today():
    # UTC 2026-11-22 16:30 已是上海的 2026-11-23 00:30
    now = datetime(2026, 11, 22, 16, 30, tzinfo=timezone.utc)
    payload = _hot_topics([_entry(1, "标题")])
    assert convert_hot_topics(payload, now)["date"] == "2026-11-23"


def test_convert_returns_none_when_nothing_usable():
    assert convert_hot_topics(b"this is not json", NOW) is None
    assert convert_hot_topics(b"{}", NOW) is None  # 没有 items
    assert convert_hot_topics(b'{"items": {}}', NOW) is None  # items 不是数组
    assert convert_hot_topics(_hot_topics([]), NOW) is None  # items 为空
    assert convert_hot_topics(_hot_topics([_entry(1, ""), _entry(2, "  ")]), NOW) is None  # 全是空标题


# ---------- 真回源（只 stub httpx.get，不真打外网） ----------


class _FakeResp:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


async def test_news_json_fetches_hot_topics_and_caches(client, cache_file, monkeypatch):
    payload = _hot_topics([_entry(1, "榜首话题"), _entry(2, "第二话题")])
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return _FakeResp(payload)

    monkeypatch.setattr(httpx, "get", fake_get)
    r = await client.get("/news.json")
    assert r.status == 200
    body = await r.json()
    assert seen["url"] == "https://aihot.news/api/v1/hot-topics"
    assert [i["title"] for i in body["items"]] == ["榜首话题", "第二话题"]
    assert all(i["category"] == "" for i in body["items"])
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

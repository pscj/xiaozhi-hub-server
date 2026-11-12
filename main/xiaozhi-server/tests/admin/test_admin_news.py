"""信息屏新闻接口 GET /news.json：不用登录，缓存优先、回源失败回退旧缓存、无缓存时 503。

回源一律 monkeypatch 掉，不真打外网。
"""

import json
import os
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.admin.admin_handler import AdminHandler

NEWS = {"date": "2026-11-12", "items": [{"category": "模型", "title": "标题", "summary": "摘要"}]}


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

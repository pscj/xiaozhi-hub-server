"""天气设置、凭据保护及天气查询错误的回归测试，不访问外部服务。"""

import json
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.admin import config_store, pages
from core.admin.admin_handler import AdminHandler
from core.utils.cache.manager import cache_manager, CacheType
from plugins_func.functions import get_weather as weather


VALUES = {"api_key": "test-weather-key-12345678", "api_host": "test.re.qweatherapi.com",
          "default_location": "杭州"}


@pytest.fixture
def cfg_path(tmp_path):
    path = tmp_path / ".config.yaml"
    path.write_text("# 保留用户注释\nplugins:\n  custom_plugin:\n    enabled: true\n", encoding="utf-8")
    return path


def merged(path):
    return config_store.load_merged(custom_path=path)


def test_weather_starts_empty_and_reports_plugin_unconfigured(cfg_path):
    config = merged(cfg_path)
    assert config["plugins"]["get_weather"] == {"api_key": "", "api_host": "", "default_location": ""}
    assert pages.describe_weather(config) == {"api_host": "", "default_location": "", "key_set": False,
                                             "key_hint": "", "configured": False}
    plugin = next(p for p in pages.describe_intent(config)["plugins"] if p["name"] == "get_weather")
    assert plugin["needs_config"] is True


def test_weather_save_masks_key_preserves_other_settings_and_keeps_blank_key(cfg_path):
    assert set(pages.save_weather(VALUES, merged(cfg_path), cfg_path)) == set(VALUES)
    config = merged(cfg_path)
    assert config["plugins"]["custom_plugin"]["enabled"] is True
    assert "# 保留用户注释" in cfg_path.read_text(encoding="utf-8")
    described = pages.describe_weather(config)
    assert described["configured"] and described["key_set"]
    assert VALUES["api_key"] not in json.dumps(described)
    assert pages.plugin_configured(config, "get_weather")
    assert pages.save_weather({**VALUES, "api_key": ""}, config, cfg_path) == []
    assert pages.save_weather({"default_location": "上海"}, config, cfg_path) == ["default_location"]
    assert merged(cfg_path)["plugins"]["get_weather"]["api_key"] == VALUES["api_key"]


@pytest.mark.parametrize("overrides, message", [
    ({"api_key": ""}, "API Key"),
    ({"api_key": "a b"}, "空格"),
    ({"api_host": ""}, "API Host"),
    ({"api_host": "https://test.re.qweatherapi.com"}, "只填写域名"),
    ({"api_host": "test.re.qweatherapi.com/path"}, "只填写域名"),
    ({"api_host": "test.re.qweatherapi.com:443"}, "只填写域名"),
    ({"api_host": []}, "必须是文本"),
    ({"default_location": ""}, "默认城市"),
])
def test_invalid_weather_settings_do_not_write(cfg_path, overrides, message):
    before = cfg_path.read_bytes()
    with pytest.raises(ValueError, match=message):
        pages.save_weather({**VALUES, **overrides}, merged(cfg_path), cfg_path)
    assert cfg_path.read_bytes() == before


async def test_weather_endpoints_auth_save_apply_and_test_without_saving(cfg_path, monkeypatch):
    running = {"plugins": {"custom_plugin": {"enabled": True}}}
    app = web.Application()
    AdminHandler(running, config_path=cfg_path).register(app)
    calls = []

    async def fake_city(location, key, host):
        calls.append((location, key, host))
        if key == "bad-key":
            raise weather.WeatherServiceError("和风天气认证失败")
        return {"name": location}

    monkeypatch.setattr(weather, "fetch_city_info", fake_city)
    async with TestClient(TestServer(app)) as client:
        for method, endpoint in (("get", "/admin/api/weather"), ("post", "/admin/api/weather"),
                                 ("post", "/admin/api/weather/test")):
            response = await getattr(client, method)(endpoint, **({"json": VALUES} if method == "post" else {}))
            assert response.status == 401
        await client.post("/admin/api/setup", json={"password": "pass-1234"})
        before = cfg_path.read_bytes()
        response = await client.post("/admin/api/weather/test", json=VALUES)
        assert response.status == 200 and "杭州" in (await response.json())["note"]
        assert cfg_path.read_bytes() == before and "get_weather" not in running["plugins"]
        response = await client.post("/admin/api/weather/test", json={**VALUES, "api_key": "bad-key"})
        assert response.status == 400 and "认证失败" in (await response.json())["error"]
        cache_manager.set(CacheType.WEATHER, "old-weather", "旧天气")
        try:
            response = await client.post("/admin/api/weather", json=VALUES)
            result = await response.json()
            assert response.status == 200 and result["changed"]
            assert "重新连接" in result["note"]
            assert running["plugins"]["get_weather"] == VALUES
            assert running["plugins"]["custom_plugin"]["enabled"]
            assert cache_manager.get(CacheType.WEATHER, "old-weather") is None
            response = await client.get("/admin/api/weather")
            assert VALUES["api_key"] not in await response.text()
            response = await client.post("/admin/api/weather/test", json={**VALUES, "api_key": ""})
            assert response.status == 200 and calls[-1] == ("杭州", VALUES["api_key"], VALUES["api_host"])
            response = await client.post("/admin/api/weather", json={**VALUES, "api_host": "bad/host"})
            assert response.status == 400
            assert running["plugins"]["get_weather"] == VALUES
        finally:
            cache_manager.clear(CacheType.WEATHER)


def mock_http(monkeypatch, status, data=None, error=None):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if error:
                raise error
            return httpx.Response(status, json=data, request=httpx.Request("GET", url))

    monkeypatch.setattr(weather.httpx, "AsyncClient", Client)
    return calls


async def test_city_lookup_uses_api_key_query_without_auth_header(monkeypatch):
    calls = mock_http(monkeypatch, 200, {"code": "200", "location": [{"name": "杭州"}]})
    assert await weather.fetch_city_info("杭州", VALUES["api_key"], VALUES["api_host"]) == {"name": "杭州"}
    url, request = calls[0]
    assert "?" not in url and VALUES["api_key"] not in url
    assert "X-QW-Api-Key" not in request["headers"]
    assert "Authorization" not in request["headers"]
    assert request["params"] == {"key": VALUES["api_key"], "location": "杭州", "lang": "zh"}


@pytest.mark.parametrize("status, message", [(401, "认证失败"), (403, "拒绝访问"), (500, "请求和风天气失败")])
async def test_weather_auth_errors_are_not_city_not_found(monkeypatch, status, message):
    mock_http(monkeypatch, status, {"error": {"detail": "Authentication failed"}})
    conn = SimpleNamespace(config={"plugins": {"get_weather": VALUES}}, client_ip=None)
    cache_manager.clear(CacheType.WEATHER)
    result = await weather.get_weather(conn, location="杭州")
    assert message in result.result and "未找到相关的城市" not in result.result
    assert VALUES["api_key"] not in result.result


async def test_weather_timeout_is_readable(monkeypatch):
    mock_http(monkeypatch, 200, error=httpx.ConnectTimeout("secret-in-request"))
    with pytest.raises(weather.WeatherServiceError, match="超时") as error:
        await weather.fetch_city_info("杭州", VALUES["api_key"], VALUES["api_host"])
    assert "secret-in-request" not in str(error.value)


async def test_weather_without_credentials_or_location_does_not_make_requests(monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("配置缺失时不应访问天气服务")

    monkeypatch.setattr(weather, "fetch_city_info", unexpected)
    conn = SimpleNamespace(config={}, client_ip=None)
    result = await weather.get_weather(conn, location="杭州")
    assert "尚未配置" in result.result
    conn.config = {"plugins": {"get_weather": {**VALUES, "default_location": ""}}}
    result = await weather.get_weather(conn)
    assert "请告诉我城市名称" in result.result

"""天气设置、凭据保护及天气查询错误的回归测试，不访问外部服务。"""

import json
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bs4 import BeautifulSoup

from core.admin import config_store, pages
from core.admin.admin_handler import AdminHandler
from core.utils.cache.manager import cache_manager, CacheType
from plugins_func.functions import get_weather as weather


VALUES = {"api_key": "test-weather-key-12345678", "api_host": "test.re.qweatherapi.com",
          "default_location": "杭州"}
FETCH_CITY_INFO = weather.fetch_city_info
FETCH_WEATHER_PAGE = weather.fetch_weather_page


@pytest.fixture(autouse=True)
def isolated_weather_cache():
    cache_manager.clear(CacheType.WEATHER)
    yield
    cache_manager.clear(CacheType.WEATHER)


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


@pytest.fixture
def weather_source(monkeypatch):
    calls = []

    async def city(location, key, host):
        calls.append((location, key, host))
        return {"name": location, "id": "101210101", "fxLink": "https://www.qweather.com/weather/test.html"}

    async def page(url):
        rows = "".join(f'''<div class="city-forecast-tabs__row">
            <div class="date-bg"><span class="date">11月{12 + day}日</span><img class="icon" src="/icons/100.svg"></div>
            <div class="tmp-cont"><span class="temp">20°</span><span class="temp">10°</span></div>
            </div>''' for day in range(8))
        return BeautifulSoup('''<h1 class="c-submenu__location">杭州</h1>
            <div class="c-city-weather-current"><div class="current-abstract">15° 晴</div>
            <div class="current-basic"><div class="current-basic___item">60% 湿度</div></div></div>'''
                             + rows, "html.parser")

    monkeypatch.setattr(weather, "fetch_city_info", city)
    monkeypatch.setattr(weather, "fetch_weather_page", page)
    return calls


def configure_weather(cfg_path):
    pages.save_weather(VALUES, merged(cfg_path), cfg_path)


async def test_client_weather_shares_structured_data_and_cache_with_voice(cfg_path, weather_source):
    configure_weather(cfg_path)
    app = web.Application()
    AdminHandler({}, config_path=cfg_path).register(app)
    async with TestClient(TestServer(app)) as client:
        # 不登录管理后台、不建立语音连接，直接读取配置的默认城市。
        response = await client.get("/kiosk/weather")
        assert response.status == 200
        body = await response.json()
        data = body["weather"]
        assert body["ok"] and data["cached"] is False
        assert data["location"] == {"id": "101210101", "name": "杭州"}
        assert data["current"] == {"summary": "15° 晴", "details": {"湿度": "60%"}}
        assert len(data["forecast"]) == 7
        assert data["forecast"][0] == {"date": "11月12日", "text": "晴", "temp_max": "20°", "temp_min": "10°"}
        assert data["fetched_at"] > 0 and "未来7天预报" in data["report"]
        assert data["source"] == "和风天气"
        assert VALUES["api_key"] not in json.dumps(body) and VALUES["api_host"] not in json.dumps(body)
        assert response.headers["Cache-Control"] == "no-store"
        response = await client.get("/kiosk/weather", params={"location": " 杭州 "})
        cached = (await response.json())["weather"]
        assert cached["cached"] and cached["fetched_at"] == data["fetched_at"]
        conn = SimpleNamespace(config=merged(cfg_path), client_ip=None)
        reply = await weather.get_weather(conn, "杭州")
        assert reply.result == data["report"]
        assert weather_source == [("杭州", VALUES["api_key"], VALUES["api_host"])]
        response = await client.get("/kiosk/weather", params={"location": "北京"})
        assert response.status == 200 and weather_source[-1][0] == "北京"


@pytest.mark.parametrize("location", ["a" * 129, "杭州\n北京", "杭州\x00"])
async def test_client_weather_invalid_location_rejected_before_request(cfg_path, weather_source, location):
    configure_weather(cfg_path)
    app = web.Application()
    AdminHandler({}, config_path=cfg_path).register(app)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/kiosk/weather", params={"location": location})
        assert response.status == 400 and (await response.json())["code"] == "invalid_location"
        assert weather_source == []


async def test_client_weather_missing_configuration_and_default_city(cfg_path, weather_source):
    app = web.Application()
    AdminHandler({}, config_path=cfg_path).register(app)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/kiosk/weather", params={"location": "杭州"})
        assert response.status == 503 and (await response.json())["code"] == "weather_not_configured"
        configure_weather(cfg_path)
        with config_store.edit_custom(cfg_path) as custom:
            custom["plugins"]["get_weather"]["default_location"] = ""
        response = await client.get("/kiosk/weather")
        assert response.status == 400 and (await response.json())["code"] == "location_required"
        assert weather_source == []
        # 显式城市不要求存在默认城市，配置从磁盘读取，无需设备重连。
        response = await client.get("/kiosk/weather", params={"location": "杭州"})
        assert response.status == 200


@pytest.mark.parametrize("kind, status, code", [
    ("not_found", 404, "location_not_found"),
    ("auth", 502, "weather_upstream_error"),
    ("city_timeout", 504, "weather_timeout"),
    ("page_timeout", 504, "weather_timeout"),
    ("page_http", 502, "weather_upstream_error"),
    ("page_changed", 502, "weather_upstream_error"),
    ("city_shape", 502, "weather_upstream_error"),
])
async def test_client_weather_failures_are_json_and_not_cached(cfg_path, weather_source, monkeypatch, kind, status, code):
    configure_weather(cfg_path)
    calls = []
    http_calls = []
    real_city = weather.fetch_city_info

    async def city(location, key, host):
        calls.append(location)
        if kind == "not_found":
            return None
        if kind == "city_shape":
            return {"name": "杭州"}
        return await real_city(location, key, host)

    monkeypatch.setattr(weather, "fetch_city_info", city)
    if kind in ("auth", "city_timeout", "page_timeout", "page_http"):
        # 保留真实错误处理，只用 HTTP 桩替换网络请求。
        if kind in ("auth", "city_timeout"):
            monkeypatch.setattr(weather, "fetch_city_info", FETCH_CITY_INFO)
        else:
            monkeypatch.setattr(weather, "fetch_weather_page", FETCH_WEATHER_PAGE)
        error = httpx.ReadTimeout(VALUES["api_key"]) if "timeout" in kind else None
        http_calls = mock_http(monkeypatch, 401 if kind == "auth" else 500, {}, error=error)
    if kind == "page_changed":
        async def broken_page(url):
            return BeautifulSoup("<html><h1>页面已更改</h1></html>", "html.parser")
        monkeypatch.setattr(weather, "fetch_weather_page", broken_page)
    app = web.Application()
    AdminHandler({}, config_path=cfg_path).register(app)
    async with TestClient(TestServer(app)) as client:
        for _ in range(2):
            response = await client.get("/kiosk/weather")
            body = await response.json()
            assert response.status == status
            assert body["ok"] is False and body["code"] == code
            assert "weather" not in body and VALUES["api_key"] not in json.dumps(body)
        # 失败后重新请求仍然查询源服务，不将错误当成天气缓存。
        assert cache_manager.get(CacheType.WEATHER, "weather_data_杭州_zh_CN") is None
        if kind in ("auth", "city_timeout", "page_timeout", "page_http"):
            assert len(http_calls) == 2
        else:
            assert len(calls) == 2

import httpx
import time
from bs4 import BeautifulSoup
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.utils.util import get_ip_info
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

GET_WEATHER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "获取某个地点的天气，用户应提供一个位置，比如用户说杭州天气，参数为：杭州。"
            "如果用户说的是省份，默认用省会城市。如果用户说的不是省份或城市而是一个地名，默认用该地所在省份的省会城市。"
            "如果上下文中已有所需地点和日期的天气可直接使用，否则调用此工具查询。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "地点名，例如杭州。可选参数，如果不提供则不传",
                },
                "lang": {
                    "type": "string",
                    "description": "返回用户使用的语言code，例如zh_CN/zh_HK/en_US/ja_JP等，默认zh_CN",
                },
            },
            "required": ["lang"],
        },
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36"
    )
}

# 天气代码 https://dev.qweather.com/docs/resource/icons/#weather-icons
WEATHER_CODE_MAP = {
    "100": "晴",
    "101": "多云",
    "102": "少云",
    "103": "晴间多云",
    "104": "阴",
    "150": "晴",
    "151": "多云",
    "152": "少云",
    "153": "晴间多云",
    "300": "阵雨",
    "301": "强阵雨",
    "302": "雷阵雨",
    "303": "强雷阵雨",
    "304": "雷阵雨伴有冰雹",
    "305": "小雨",
    "306": "中雨",
    "307": "大雨",
    "308": "极端降雨",
    "309": "毛毛雨/细雨",
    "310": "暴雨",
    "311": "大暴雨",
    "312": "特大暴雨",
    "313": "冻雨",
    "314": "小到中雨",
    "315": "中到大雨",
    "316": "大到暴雨",
    "317": "暴雨到大暴雨",
    "318": "大暴雨到特大暴雨",
    "350": "阵雨",
    "351": "强阵雨",
    "399": "雨",
    "400": "小雪",
    "401": "中雪",
    "402": "大雪",
    "403": "暴雪",
    "404": "雨夹雪",
    "405": "雨雪天气",
    "406": "阵雨夹雪",
    "407": "阵雪",
    "408": "小到中雪",
    "409": "中到大雪",
    "410": "大到暴雪",
    "456": "阵雨夹雪",
    "457": "阵雪",
    "499": "雪",
    "500": "薄雾",
    "501": "雾",
    "502": "霾",
    "503": "扬沙",
    "504": "浮尘",
    "507": "沙尘暴",
    "508": "强沙尘暴",
    "509": "浓雾",
    "510": "强浓雾",
    "511": "中度霾",
    "512": "重度霾",
    "513": "严重霾",
    "514": "大雾",
    "515": "特强浓雾",
    "900": "热",
    "901": "冷",
    "999": "未知",
}


class WeatherServiceError(Exception):
    """可直接展示给用户的天气服务错误，不包含请求凭据。"""

    def __init__(self, message, *, code="weather_upstream_error", status=502):
        super().__init__(message)
        self.code = code
        self.status = status


def _weather_options(weather_config):
    api_host = str(weather_config.get("api_host") or "").strip()
    api_key = str(weather_config.get("api_key") or "").strip()
    default_location = str(weather_config.get("default_location") or "").strip()
    if not api_host or not api_key:
        raise WeatherServiceError(
            "天气服务尚未配置，请先在管理后台的“意图识别 → 查天气 → 设置”中填写 API Key 和 API Host",
            code="weather_not_configured", status=503,
        )
    return api_host, api_key, default_location


async def fetch_city_info(location, api_key, api_host):
    url = f"https://{api_host}/geo/v2/city/lookup"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            response = await client.get(url, headers=HEADERS,
                                        params={"key": api_key, "location": location, "lang": "zh"})
        if response.status_code == 401:
            raise WeatherServiceError("和风天气认证失败，请检查天气设置中的 API Key 和 API Host 是否正确且属于同一账号")
        if response.status_code == 403:
            raise WeatherServiceError("和风天气拒绝访问，请检查 API Host、账号额度和凭据访问限制")
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException:
        raise WeatherServiceError("连接和风天气超时，请稍后重试", code="weather_timeout", status=504) from None
    except httpx.HTTPError:
        raise WeatherServiceError("请求和风天气失败，请检查 API Host 和网络连接") from None
    except ValueError:
        raise WeatherServiceError("和风天气返回了无法解析的数据，请检查 API Host") from None
    if not isinstance(data, dict):
        raise WeatherServiceError("和风天气返回的城市数据格式不正确")
    code = str(data.get("code", "200"))
    if data.get("error") is not None or code not in ("200", "404"):
        raise WeatherServiceError("和风天气查询失败，请检查天气设置中的凭据、API Host 和账号状态")
    locations = data.get("location") or []
    if not isinstance(locations, list):
        raise WeatherServiceError("和风天气返回的城市数据格式不正确")
    return locations[0] if locations else None


async def fetch_weather_page(url):
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = await client.get(url, headers=HEADERS)
    except httpx.TimeoutException:
        raise WeatherServiceError("获取天气页面超时，请稍后重试", code="weather_timeout", status=504) from None
    except httpx.HTTPError:
        raise WeatherServiceError("获取天气页面失败，请稍后重试") from None
    return BeautifulSoup(response.text, "html.parser") if response.status_code == 200 else None


def parse_weather_info(soup):
    city_name = soup.select_one("h1.c-submenu__location").get_text(strip=True)

    current_abstract = soup.select_one(".c-city-weather-current .current-abstract")
    current_abstract = (
        current_abstract.get_text(strip=True) if current_abstract else "未知"
    )

    current_basic = {}
    for item in soup.select(
        ".c-city-weather-current .current-basic .current-basic___item"
    ):
        parts = item.get_text(strip=True, separator=" ").split(" ")
        if len(parts) == 2:
            key, value = parts[1], parts[0]
            current_basic[key] = value

    temps_list = []
    for row in soup.select(".city-forecast-tabs__row")[:7]:  # 取前7天的数据
        date = row.select_one(".date-bg .date").get_text(strip=True)
        weather_code = (
            row.select_one(".date-bg .icon")["src"].split("/")[-1].split(".")[0]
        )
        weather = WEATHER_CODE_MAP.get(weather_code, "未知")
        temps = [span.get_text(strip=True) for span in row.select(".tmp-cont .temp")]
        high_temp, low_temp = (temps[0], temps[-1]) if len(temps) >= 2 else (None, None)
        temps_list.append((date, weather, high_temp, low_temp))

    return city_name, current_abstract, current_basic, temps_list


async def get_weather_data(weather_config: dict, location: str = None, lang: str = "zh_CN") -> dict:
    """供语音插件和客户端 API 共用的天气查询，不依赖设备连接或大模型。"""
    from core.utils.cache.manager import cache_manager, CacheType

    api_host, api_key, default_location = _weather_options(weather_config)
    location = (location or "").strip() or default_location
    if not location:
        raise WeatherServiceError(
            "无法确定查询城市，请告诉我城市名称，或在管理后台的“意图识别 → 查天气 → 设置”中填写默认城市",
            code="location_required", status=400,
        )
    if len(location) > 128 or any(ord(c) < 32 for c in location):
        raise WeatherServiceError("location 不能超过 128 个字符或包含控制字符", code="invalid_location", status=400)
    weather_cache_key = f"weather_data_{location}_{lang}"
    cached_weather = cache_manager.get(CacheType.WEATHER, weather_cache_key)
    if cached_weather:
        return {**cached_weather, "cached": True}

    city_info = await fetch_city_info(location, api_key, api_host)
    if not city_info:
        raise WeatherServiceError(
            f"未找到相关的城市: {location}，请确认地点是否正确", code="location_not_found", status=404,
        )
    if not isinstance(city_info, dict) or not city_info.get("fxLink"):
        raise WeatherServiceError("和风天气返回的城市数据不完整，请稍后重试")
    source_url = city_info["fxLink"]
    soup = await fetch_weather_page(source_url)
    if not soup:
        raise WeatherServiceError("获取天气页面失败，请稍后重试")
    try:
        city_name, current_abstract, current_basic, temps_list = parse_weather_info(soup)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        raise WeatherServiceError("天气页面数据格式发生变化，暂时无法读取天气") from None
    if not city_name or (current_abstract == "未知" and not temps_list):
        raise WeatherServiceError("天气页面缺少有效天气数据，请稍后重试")

    weather_report = f"您查询的位置是：{city_name}\n\n当前天气: {current_abstract}\n"

    # 添加有效的当前天气参数
    if current_basic:
        weather_report += "详细参数：\n"
        for key, value in current_basic.items():
            if value != "0":  # 过滤无效值
                weather_report += f"  · {key}: {value}\n"

    # 添加7天预报
    weather_report += "\n未来7天预报：\n"
    for date, weather, high, low in temps_list:
        weather_report += f"{date}: {weather}，气温 {low}~{high}\n"

    # 提示语
    weather_report += "\n（如需某一天的具体天气，请告诉我日期）"

    weather_data = {
        "location": {"id": city_info.get("id"), "name": city_name},
        "current": {"summary": current_abstract, "details": current_basic},
        "forecast": [{"date": date, "text": weather, "temp_max": high, "temp_min": low}
                     for date, weather, high, low in temps_list],
        "report": weather_report,
        "source": "和风天气",
        "source_url": source_url,
        "fetched_at": int(time.time()),
    }
    cache_manager.set(CacheType.WEATHER, weather_cache_key, weather_data)
    return {**weather_data, "cached": False}


@register_function("get_weather", GET_WEATHER_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def get_weather(conn: "ConnectionHandler", location: str = None, lang: str = "zh_CN"):
    from core.utils.cache.manager import cache_manager, CacheType

    weather_config = conn.config.get("plugins", {}).get("get_weather", {})
    try:
        _weather_options(weather_config)
        # 语音查询保留原有的 IP 定位；客户端 API 使用显式城市或后台默认城市。
        if not location and conn.client_ip:
            ip_info = cache_manager.get(CacheType.IP_INFO, conn.client_ip)
            if ip_info is None:
                ip_info = get_ip_info(conn.client_ip, logger)
                if ip_info:
                    cache_manager.set(CacheType.IP_INFO, conn.client_ip, ip_info)
            if ip_info:
                location = ip_info.get("city")
        data = await get_weather_data(weather_config, location, lang)
    except WeatherServiceError as e:
        logger.bind(tag=TAG).error(str(e))
        return ActionResponse(Action.REQLLM, str(e), None)
    return ActionResponse(Action.REQLLM, data["report"], None)

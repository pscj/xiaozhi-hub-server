"""Muse 通道闹钟桥：Muse 回合没有工具回路，靠回复第一行的结构化标记建闹钟。

约定（跨仓契约，客户端工具名不可改）：Muse 在回合回复的**第一行**单独写一行
``【闹钟】{"action":"create","time":"07:30","repeat":"daily","label":"起床"}``，
action ∈ create/list/cancel/set_enabled，其余字段与设备工具
``self.alarm.*`` 的参数同名。桥在 MuseSession.send() 发射回复文本之前处理
第一段文本：剥掉标记行（绝不让它流进 TTS 和对话记录），再对本回合的设备连接
程序化调用 device_mcp 的 call_mcp_tool，结果用一句补播报经待汇报箱投递。

顺序与经典 function_call 链不同：Muse 先回复、桥后执行，所以口头承诺是乐观
的，失败靠补播报纠正（「闹钟没设成：…」）。经典 LLM 回合不经过本桥。
"""

import asyncio
import json
import re
from datetime import datetime

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

MARKER = "【闹钟】"
BOT_ID = "alarm"  # 待汇报箱里标记是谁的话

ACTION_TOOLS = {
    "create": "self.alarm.create",
    "list": "self.alarm.list",
    "cancel": "self.alarm.cancel",
    "set_enabled": "self.alarm.set_enabled",
}
_REPEAT_TYPES = ("once", "daily", "weekdays", "weekly", "monthly")
_FAIL_PREFIX = {
    "create": "闹钟没设成",
    "list": "闹钟列表没查成",
    "cancel": "闹钟没取消成",
    "set_enabled": "闹钟开关没改成",
}
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_WEEKDAY_NAMES = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}


# ---------- 标记提取 ----------

def extract(text):
    """从回合回复文本里提取闹钟标记。

    返回 (剥除标记行后的文本, spec, error)：
    - 第一行（容忍前置空行与行内首尾空白）不是标记 → (原文, None, None)，不触发；
    - 是标记且 JSON/动作/必填字段都合法 → (余文, spec dict, None)；
    - 是标记但内容有问题 → (余文, None, 失败原因)，标记行照样剥掉、走失败补播报。
    """
    if not text:
        return text, None, None
    lines = text.split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return text, None, None
    first = lines[idx].strip()
    if not first.startswith(MARKER):
        return text, None, None
    rest = "\n".join(lines[idx + 1:]).strip()
    payload = first[len(MARKER):].strip()
    if not payload:
        return rest, None, "标记后面没有内容"
    try:
        spec = json.loads(payload)
    except (ValueError, TypeError):
        return rest, None, "标记内容不是有效的 JSON"
    if not isinstance(spec, dict):
        return rest, None, "标记内容格式不对"
    error = _validate(spec)
    if error:
        return rest, None, error
    return rest, spec, None


def _validate(spec):
    action = spec.get("action")
    if not action:
        return "标记里缺少 action"
    if action not in ACTION_TOOLS:
        return f"不认识的闹钟动作「{action}」"
    if action == "create":
        time_value = spec.get("time")
        if not time_value:
            return "缺少时间"
        if not _TIME_RE.match(str(time_value).strip()):
            return "时间格式不对，应是几点几分"
        repeat = spec.get("repeat")
        if repeat is not None and repeat not in _REPEAT_TYPES:
            return f"不认识的重复方式「{repeat}」"
    if action in ("cancel", "set_enabled"):
        if not spec.get("id"):
            return "缺少闹钟编号"
    if action == "set_enabled":
        if "enabled" not in spec:
            return "缺少开关状态"
        enabled = spec["enabled"]
        if isinstance(enabled, str) and enabled.strip().lower() in ("true", "false"):
            spec["enabled"] = enabled.strip().lower() == "true"
        elif not isinstance(enabled, bool):
            return "开关状态格式不对"
    return None


def build_args(spec):
    """spec → 设备工具参数（只带给定的字段，缺省语义留给设备端）。"""
    action = spec["action"]
    if action == "create":
        args = {"time": str(spec["time"]).strip()}
        for key in ("repeat", "weekdays", "month_day", "label"):
            if spec.get(key) is not None:
                args[key] = spec[key]
        return args
    if action == "cancel":
        return {"id": spec["id"]}
    if action == "set_enabled":
        return {"id": spec["id"], "enabled": bool(spec["enabled"])}
    return {}


# ---------- 播报文案（全部由工具返回的数据生成） ----------

def time_text(value):
    """ "07:30" → "7点30分"；"07:00" → "7点"；认不出的原样返回。"""
    match = _TIME_RE.match(str(value or "").strip())
    if not match:
        return str(value or "")
    hour, minute = int(match.group(1)), int(match.group(2))
    return f"{hour}点{minute}分" if minute else f"{hour}点"


def repeat_text(repeat):
    """循环规则 → 口语说法。repeat 可以是字符串或 {type, weekdays, month_day}。"""
    if repeat is None:
        return "只响一次"
    if isinstance(repeat, str):
        repeat = {"type": repeat}
    if not isinstance(repeat, dict):
        return "只响一次"
    kind = repeat.get("type") or "once"
    if kind == "daily":
        return "每天"
    if kind == "weekdays":
        return "工作日"
    if kind == "weekly":
        names = []
        for day in repeat.get("weekdays") or []:
            try:
                name = _WEEKDAY_NAMES.get(int(day))
            except (TypeError, ValueError):
                name = None
            if name and name not in names:
                names.append(name)
        return "每" + "、".join(names) if names else "每周"
    if kind == "monthly":
        day = repeat.get("month_day")
        try:
            return f"每月{int(day)}号"
        except (TypeError, ValueError):
            return "每月"
    return "只响一次"


def next_fire_text(value, now=None):
    """ "2026-11-20T07:30:00" → "明天7点30分" / "今天…" / "11月23日7点30分"。"""
    try:
        moment = datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return str(value or "")
    now = now or datetime.now()
    days = (moment.date() - now.date()).days
    day = {0: "今天", 1: "明天", 2: "后天"}.get(days)
    if day is None:
        day = f"{moment.month}月{moment.day}日"
    return day + time_text(f"{moment.hour:02d}:{moment.minute:02d}")


def describe_item(item):
    """一条闹钟 → 一句可念的描述，如「起床，7点30分，每天，已关闭」。"""
    if not isinstance(item, dict):
        return str(item)
    label = str(item.get("label") or "闹钟")
    parts = [label, time_text(item.get("time"))]
    repeat = repeat_text(item.get("repeat"))
    if repeat != "只响一次":
        parts.append(repeat)
    if item.get("enabled") is False:
        parts.append("已关闭")
    return "，".join(p for p in parts if p)


def success_text(spec, data, now=None):
    """工具调用成功后要念的话。data 是设备返回 JSON 解析出的 dict/list。"""
    action = spec["action"]
    if action == "create":
        item = data if isinstance(data, dict) else {}
        label = item.get("label") or spec.get("label") or ""
        parts = [str(label), time_text(item.get("time") or spec.get("time"))]
        repeat = repeat_text(item.get("repeat") or spec.get("repeat"))
        if repeat != "只响一次":
            parts.append(repeat)
        text = "闹钟已设好"
        seg = "，".join(p for p in parts if p)
        if seg:
            text += f"，{seg}"
        next_fire = item.get("next_fire")
        if next_fire:
            text += f"，下一次是{next_fire_text(next_fire, now)}"
        return text + "。"
    if action == "list":
        items = _result_items(data)
        if not items:
            return "你现在没有设闹钟。"
        joined = "；".join(describe_item(item) for item in items)
        return f"你一共有{len(items)}个闹钟：{joined}。"
    if action == "cancel":
        item = data if isinstance(data, dict) else {}
        label = item.get("label") or ""
        when = time_text(item.get("time")) if item.get("time") else ""
        seg = "".join(p for p in (str(label), when) if p)
        return f"已经取消了{seg}的闹钟。" if seg else "闹钟已经取消了。"
    if action == "set_enabled":
        if spec.get("enabled"):
            item = data if isinstance(data, dict) else {}
            next_fire = item.get("next_fire")
            if next_fire:
                return f"闹钟已打开，下一次是{next_fire_text(next_fire, now)}。"
            return "闹钟已打开。"
        return "闹钟已关闭。"
    return "闹钟操作完成了。"


def failure_text(spec, reason):
    action = spec.get("action") if isinstance(spec, dict) else None
    return f"{_FAIL_PREFIX.get(action, '闹钟没设成')}：{reason}。"


def _result_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("alarms", "items", "list"):
            items = data.get(key)
            if isinstance(items, list):
                return items
    return []


def _result_error(data):
    """设备返回 success=false / error 时的原因；不是失败返回 None。"""
    if not isinstance(data, dict):
        return None
    if data.get("success") is False or (data.get("error") and data.get("success") is not True):
        return str(data.get("error") or data.get("message") or "设备返回失败")
    return None


def _parse_result(raw):
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, (dict, list)) else None
    return None


def _exception_reason(exc):
    if isinstance(exc, ValueError):
        return "设备上还没有闹钟功能"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "设备没有回应"
    if isinstance(exc, RuntimeError):
        message = str(exc)
        if "工具调用错误" in message:
            detail = message.split(":", 1)[-1].split("：", 1)[-1].strip()
            return detail or "设备那边出错了"
        if "尚未准备就绪" in message or "尚未就绪" in message:
            return "设备还没准备好"
        return message[:80] or "设备那边出错了"
    return str(exc)[:80] or "设备那边出错了"


# ---------- 执行：找连接 → 调工具 → 补播报 ----------

def announce(device_key, text):
    """把一句结果放进待汇报箱（可从任何线程/循环调用，失败只记日志）。"""
    if not device_key or not text:
        return
    try:
        from core.agent import reports

        reports.add(device_key, BOT_ID, text)
    except Exception:
        logger.bind(tag=TAG).warning(f"闹钟结果放入待汇报箱失败，设备={device_key}")


def _find_conn(device_key):
    from core.admin import connections
    from core.agent import reports

    for item in connections.snapshot():
        conn = connections.get(item["session_id"])
        if conn is not None and reports.device_key(conn) == device_key:
            return conn
    return None


async def execute(device_key, spec):
    """执行一条闹钟标记：调设备工具，结果（成败都）用补播报投递。永不抛异常。"""
    try:
        text = await _execute_inner(device_key, spec)
    except Exception as exc:  # noqa: BLE001 —— 桥出任何问题都不能影响回合本身
        logger.bind(tag=TAG).warning(f"闹钟桥执行异常：{exc!r}")
        text = failure_text(spec, _exception_reason(exc))
    announce(device_key, text)


async def _execute_inner(device_key, spec):
    conn = _find_conn(device_key)
    if conn is None:
        return failure_text(spec, "设备不在线")
    mcp_client = getattr(conn, "mcp_client", None)
    loop = getattr(conn, "loop", None)
    if mcp_client is None or loop is None:
        return failure_text(spec, "设备还没准备好")
    from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool
    from core.utils.util import sanitize_tool_name

    tool_name = sanitize_tool_name(ACTION_TOOLS[spec["action"]])
    args = json.dumps(build_args(spec), ensure_ascii=False)
    logger.bind(tag=TAG).info(f"闹钟桥调用设备工具 {tool_name}，参数 {args}，设备 {device_key}")
    future = asyncio.run_coroutine_threadsafe(
        call_mcp_tool(conn, mcp_client, tool_name, args), loop
    )
    raw = await asyncio.wrap_future(future)
    data = _parse_result(raw)
    if data is None:
        return failure_text(spec, "设备返回的内容看不懂")
    error = _result_error(data)
    if error:
        return failure_text(spec, error)
    return success_text(spec, data)


# ---------- 回合入口：MuseSession.send() 发射第一段回复文本时调用 ----------

def process_first_text(device_key, text):
    """处理回合回复的第一段文本。返回 (要继续往下发的文本, 是否命中了标记)。

    没标记时原样返回、零副作用；命中时标记行已被剥掉，执行异步安排，
    解析失败则安排一句失败补播报——两种情况都不会让回合崩溃。
    """
    stripped, spec, error = extract(text)
    if spec is None and error is None:
        return text, False
    if error is not None:
        logger.bind(tag=TAG).warning(f"闹钟标记解析失败：{error}，设备={device_key}")
        announce(device_key, failure_text(None, error))
        return stripped, True
    logger.bind(tag=TAG).info(f"闹钟标记命中：action={spec.get('action')}，设备={device_key}")
    if device_key:
        try:
            asyncio.ensure_future(execute(device_key, spec))
        except RuntimeError:
            logger.bind(tag=TAG).warning("闹钟桥没有可用的事件循环，跳过执行")
            announce(device_key, failure_text(spec, "设备还没准备好"))
    return stripped, True

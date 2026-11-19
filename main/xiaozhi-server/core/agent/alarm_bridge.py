"""Muse 通道标记桥：Muse 回合没有工具回路，靠回复第一行的结构化标记操作设备。

约定（跨仓契约，客户端工具名不可改）：Muse 在回合回复的**第一行**单独写一行
标记，桥在 MuseSession.send() 发射回复文本之前处理第一段文本：剥掉标记行
（绝不让它流进 TTS 和对话记录），再对本回合的设备连接程序化调用 device_mcp
的 call_mcp_tool，结果用一句补播报经待汇报箱只念不记地投递。

两个域共用同一套机制（首行前缀门 FirstLineGate、双来源检测、标记行剥除、
执行骨架、补播报），靠域注册表 DOMAINS 分发；域之间只差标记串、动作→工具
映射、参数校验与播报文案：

- 闹钟域：``【闹钟】{"action":"create","time":"07:30","repeat":"daily","label":"起床"}``，
  action ∈ create/list/cancel/set_enabled，其余字段与设备工具
  ``self.alarm.*`` 的参数同名；cancel/set_enabled 的 id 必填。
- 倒计时域：``【倒计时】{"action":"create","duration_sec":600,"label":"煮面"}``，
  action ∈ create/list/cancel/pause/resume，字段与设备工具 ``self.timer.*``
  同名；cancel/pause/resume 可省略 id——桥先内部查一次列表，活动中恰好
  一条就锁定它，多条时按 label 唯一匹配，仍定不下来走失败补播报请用户
  指明（见设计稿 countdown-design.md §7）。

顺序与经典 function_call 链不同：Muse 先回复、桥后执行，所以口头承诺是乐观
的，失败靠补播报纠正（「闹钟没设成：…」/「倒计时没设成：…」）。经典 LLM
回合不经过本桥。本模块按历史文件名保留 alarm_bridge，muse_session 的接法
（FirstLineGate/handle_marker_line/strip_marker_from_final/process_first_text
四个入口）不随域增加而变化。
"""

import asyncio
import json
import math
import re
from datetime import datetime

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# ---------- 闹钟域 ----------

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

# ---------- 倒计时域 ----------

TIMER_MARKER = "【倒计时】"
TIMER_BOT_ID = "timer"

TIMER_ACTION_TOOLS = {
    "create": "self.timer.create",
    "list": "self.timer.list",
    "cancel": "self.timer.cancel",
    "pause": "self.timer.pause",
    "resume": "self.timer.resume",
}
_TIMER_FAIL_PREFIX = {
    "create": "倒计时没设成",
    "list": "倒计时列表没查成",
    "cancel": "倒计时没取消成",
    "pause": "倒计时没暂停成",
    "resume": "倒计时没继续成",
}
_TIMER_MAX_SECONDS = 86400  # 倒计时上限 24 小时，再长该用闹钟（绝对时刻）

# 首行前缀门要认的全部标记（两域）
ALL_MARKERS = (MARKER, TIMER_MARKER)


# ---------- 标记提取 ----------

def _extract(text, marker, validate):
    """从回合回复文本里提取指定域的标记。

    返回 (剥除标记行后的文本, spec, error)：
    - 第一行（容忍前置空行与行内首尾空白）不是该域标记 → (原文, None, None)，不触发；
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
    if not first.startswith(marker):
        return text, None, None
    rest = "\n".join(lines[idx + 1:]).strip()
    payload = first[len(marker):].strip()
    if not payload:
        return rest, None, "标记后面没有内容"
    try:
        spec = json.loads(payload)
    except (ValueError, TypeError):
        return rest, None, "标记内容不是有效的 JSON"
    if not isinstance(spec, dict):
        return rest, None, "标记内容格式不对"
    error = validate(spec)
    if error:
        return rest, None, error
    return rest, spec, None


def extract(text):
    """从回合回复文本里提取闹钟标记（规则见 _extract）。"""
    return _extract(text, MARKER, _validate)


def extract_timer(text):
    """从回合回复文本里提取倒计时标记（规则见 _extract）。"""
    return _extract(text, TIMER_MARKER, _validate_timer)


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


def _validate_timer(spec):
    action = spec.get("action")
    if not action:
        return "标记里缺少 action"
    if action not in TIMER_ACTION_TOOLS:
        return f"不认识的倒计时动作「{action}」"
    if action == "create":
        value = spec.get("duration_sec")
        if value is None:
            return "缺少时长"
        if isinstance(value, bool):
            return "时长格式不对，应是总秒数"
        if isinstance(value, int):
            seconds = value
        elif isinstance(value, float) and value.is_integer():
            seconds = int(value)
        elif isinstance(value, str) and value.strip().isdigit():
            seconds = int(value.strip())
        else:
            return "时长格式不对，应是总秒数"
        if not 1 <= seconds <= _TIMER_MAX_SECONDS:
            return "时长要在1秒到24小时之间"
        spec["duration_sec"] = seconds
    # cancel/pause/resume 的 id 可省略：执行前桥会先查列表锁定目标（见 _resolve_timer_target）
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


def build_timer_args(spec):
    """倒计时 spec → 设备工具参数。cancel/pause/resume 的 id 在执行前已由桥锁定。"""
    action = spec["action"]
    if action == "create":
        args = {"duration_sec": int(spec["duration_sec"])}
        if spec.get("label") is not None:
            args["label"] = spec["label"]
        return args
    if action in ("cancel", "pause", "resume"):
        return {"id": spec["id"]} if spec.get("id") else {}
    return {}


# ---------- 闹钟域播报文案（全部由工具返回的数据生成） ----------

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


# ---------- 倒计时域播报文案（全部由工具返回的数据生成） ----------

def duration_text(seconds):
    """总秒数 → 口语时长：45→「45秒」、600→「10分钟」、570→「9分30秒」、3660→「1小时1分」。

    规则：≥1 小时「X 小时 Y 分」（Y 为 0 省略）；≥1 分「X 分 Y 秒」（秒为 0
    时写作「X 分钟」）；<1 分「X 秒」。
    """
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return str(seconds or "")
    if total < 0:
        total = 0
    if total >= 3600:
        hours, rest = divmod(total, 3600)
        minutes = rest // 60
        return f"{hours}小时{minutes}分" if minutes else f"{hours}小时"
    if total >= 60:
        minutes, secs = divmod(total, 60)
        return f"{minutes}分{secs}秒" if secs else f"{minutes}分钟"
    return f"{total}秒"


def _clock_text(value):
    """ "2026-11-19T15:42:00" → "3点42分"（12 小时制，与设计稿播报文案一致）。

    只取时刻部分；解析不出返回空串，调用方据此省略「到点」段。
    """
    try:
        moment = datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return ""
    hour = moment.hour % 12 or 12
    return f"{hour}点{moment.minute}分" if moment.minute else f"{hour}点"


def describe_timer_item(item):
    """一条倒计时 → 一句可念的描述，如「煮面，还剩9分30秒」「泡茶，已暂停，还剩3分钟」。"""
    if not isinstance(item, dict):
        return str(item)
    label = str(item.get("label") or "倒计时")
    remaining = item.get("remaining_sec")
    if item.get("state") == "paused":
        text = f"{label}，已暂停"
        if remaining is not None:
            text += f"，还剩{duration_text(remaining)}"
        return text
    if remaining is not None:
        return f"{label}，还剩{duration_text(remaining)}"
    return label


def timer_success_text(spec, data, now=None):
    """倒计时工具调用成功后要念的话。data 是设备返回 JSON 解析出的 dict/list。"""
    action = spec["action"]
    if action == "create":
        item = data if isinstance(data, dict) else {}
        label = item.get("label") or spec.get("label") or ""
        duration = item.get("duration_sec")
        if duration is None:
            duration = spec.get("duration_sec")
        parts = [str(label), duration_text(duration) if duration is not None else ""]
        text = "倒计时已设好"
        seg = "，".join(p for p in parts if p)
        if seg:
            text += f"，{seg}"
        ends = _clock_text(item.get("ends_at_local"))
        if ends:
            text += f"，{ends}到点"
        return text + "。"
    if action == "list":
        items = _result_items(data)
        if not items:
            return "你现在没有倒计时。"
        joined = "；".join(describe_timer_item(item) for item in items)
        return f"你一共有{len(items)}个倒计时：{joined}。"
    if action == "cancel":
        label = spec.get("_label") or spec.get("label") or ""
        return f"已经取消了{label}的倒计时。" if label else "倒计时已经取消了。"
    if action == "pause":
        item = data if isinstance(data, dict) else {}
        remaining = item.get("remaining_sec")
        if remaining is not None:
            return f"倒计时已暂停，还剩{duration_text(remaining)}。"
        return "倒计时已暂停。"
    if action == "resume":
        item = data if isinstance(data, dict) else {}
        remaining = item.get("remaining_sec")
        if remaining is None and item.get("ends_at") is not None:
            try:
                now_ms = (now or datetime.now()).timestamp() * 1000
                remaining = max(0, math.ceil((float(item["ends_at"]) - now_ms) / 1000))
            except (TypeError, ValueError):
                remaining = None
        if remaining is not None:
            return f"倒计时继续，{duration_text(remaining)}后到点。"
        ends = _clock_text(item.get("ends_at_local"))
        if ends:
            return f"倒计时继续，{ends}到点。"
        return "倒计时继续了。"
    return "倒计时操作完成了。"


def timer_failure_text(spec, reason):
    action = spec.get("action") if isinstance(spec, dict) else None
    return f"{_TIMER_FAIL_PREFIX.get(action, '倒计时没设成')}：{reason}。"


# ---------- 工具返回解析（两域共用） ----------

def _result_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("alarms", "timers", "items", "list"):
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


def _exception_reason(exc, feature="闹钟"):
    if isinstance(exc, ValueError):
        return f"设备上还没有{feature}功能"
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

def announce(device_key, text, bot_id=BOT_ID):
    """把一句结果放进待汇报箱（可从任何线程/循环调用，失败只记日志）。

    桥的结果只念不记：record=False 投递——不写进设备对话、聊天记录里不留，
    设备端也不上屏（见 reports.speak 的只念不记发声）。bot_id 区分是哪个
    域的话（闹钟 "alarm" / 倒计时 "timer"）。"""
    if not device_key or not text:
        return
    try:
        from core.agent import reports

        reports.add(device_key, bot_id, text, record=False)
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


def _resolve_timer_target(spec, items):
    """cancel/pause/resume 没带 id 时，从设备 list 结果里锁定目标倒计时。

    池子按动作圈定（cancel 看 running+paused、pause 只看 running、resume
    只看 paused）：恰好一条直接锁定；带了 label 时按 label 唯一匹配。
    返回 (目标条目, None) 或 (None, 定不下来的原因)。
    """
    action = spec["action"]
    states = {"cancel": ("running", "paused"), "pause": ("running",), "resume": ("paused",)}[action]
    pool = [
        item for item in items
        if isinstance(item, dict) and item.get("id")
        and (item.get("state") or "running") in states
    ]
    label = spec.get("label")
    if label:
        named = [item for item in pool if str(item.get("label") or "") == str(label)]
        if len(named) == 1:
            return named[0], None
        if not named:
            return None, f"没有叫「{label}」的倒计时"
        return None, f"有{len(named)}个叫「{label}」的倒计时，说一下编号"
    if len(pool) == 1:
        return pool[0], None
    if not pool:
        return None, {
            "cancel": "现在没有在跑的倒计时",
            "pause": "现在没有正在跑的倒计时",
            "resume": "现在没有暂停中的倒计时",
        }[action]
    return None, f"有{len(pool)}个倒计时在跑，我分不清是哪一个，说一下名字或编号"


async def execute_timer(device_key, spec):
    """执行一条倒计时标记：调设备工具，结果（成败都）用补播报投递。永不抛异常。"""
    try:
        text = await _execute_timer_inner(device_key, spec)
    except Exception as exc:  # noqa: BLE001 —— 桥出任何问题都不能影响回合本身
        logger.bind(tag=TAG).warning(f"倒计时桥执行异常：{exc!r}")
        text = timer_failure_text(spec, _exception_reason(exc, "倒计时"))
    announce(device_key, text, bot_id=TIMER_BOT_ID)


async def _execute_timer_inner(device_key, spec):
    conn = _find_conn(device_key)
    if conn is None:
        return timer_failure_text(spec, "设备不在线")
    mcp_client = getattr(conn, "mcp_client", None)
    loop = getattr(conn, "loop", None)
    if mcp_client is None or loop is None:
        return timer_failure_text(spec, "设备还没准备好")
    from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool
    from core.utils.util import sanitize_tool_name

    async def call(tool, args):
        """调一个设备工具：成功返回 (数据, None)，设备侧失败返回 (None, 原因)。

        传输层异常（工具不存在/超时等）照常往外抛，归 execute_timer 统一归因。
        """
        tool_name = sanitize_tool_name(tool)
        payload = json.dumps(args, ensure_ascii=False)
        logger.bind(tag=TAG).info(f"倒计时桥调用设备工具 {tool_name}，参数 {payload}，设备 {device_key}")
        future = asyncio.run_coroutine_threadsafe(
            call_mcp_tool(conn, mcp_client, tool_name, payload), loop
        )
        raw = await asyncio.wrap_future(future)
        data = _parse_result(raw)
        if data is None:
            return None, "设备返回的内容看不懂"
        error = _result_error(data)
        if error:
            return None, error
        return data, None

    action = spec["action"]
    if action in ("cancel", "pause", "resume") and not spec.get("id"):
        data, error = await call("self.timer.list", {})
        if error:
            return timer_failure_text(spec, error)
        target, reason = _resolve_timer_target(spec, _result_items(data))
        if target is None:
            return timer_failure_text(spec, reason)
        spec = dict(spec)
        spec["id"] = target.get("id")
        if target.get("label"):
            spec["_label"] = target["label"]
    data, error = await call(TIMER_ACTION_TOOLS[action], build_timer_args(spec))
    if error:
        return timer_failure_text(spec, error)
    return timer_success_text(spec, data)


# ---------- 回合入口：MuseSession.send() 发射第一段回复文本时调用 ----------

def _first_content_line(text):
    """文本里第一个非空行（剥过首尾空白）；没有返回空串。"""
    for line in (text or "").split("\n"):
        if line.strip():
            return line.strip()
    return ""


def strip_marker_from_final(text, marker_line):
    """从最终文本里剥掉与标记行对应的第一行；没有对应行时原样返回。

    最终文本（display_text）对标记行的渲染与增量原文不总是一致：可能原样
    保留，也可能丢掉标记前缀、只剩 JSON 那一行（2026-11-19 08:35 实测：
    桥从增量命中、最终文本原样下发，JSON 行就这么漏进 TTS 和对话列表）。
    两种形态出现在首个非空行时都剥掉；首个非空行是别的内容，说明最终文本
    已把标记行整行丢掉、正文就是首行，不动它。两域共用：标记前缀按
    marker_line 实际带的是哪个域的标记来认。
    """
    if not text or not marker_line:
        return text
    payload = ""
    for marker in ALL_MARKERS:
        if marker_line.startswith(marker):
            payload = marker_line[len(marker):].strip()
            break
    lines = text.split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return text
    first = lines[idx].strip()
    if first == marker_line or (payload and first == payload):
        return "\n".join(lines[idx + 1:]).strip()
    return text


# ---------- 域注册表：分发只认这张表 ----------

ALARM_DOMAIN = {
    "name": "闹钟",
    "marker": MARKER,
    "bot_id": BOT_ID,
    "extract": extract,
    "failure_text": failure_text,
    "execute_name": "execute",
}
TIMER_DOMAIN = {
    "name": "倒计时",
    "marker": TIMER_MARKER,
    "bot_id": TIMER_BOT_ID,
    "extract": extract_timer,
    "failure_text": timer_failure_text,
    "execute_name": "execute_timer",
}
DOMAINS = (ALARM_DOMAIN, TIMER_DOMAIN)


def _dispatch(device_key, domain, spec, error, source):
    """命中标记后的统一出口：记日志，安排执行或失败补播报。"""
    name = domain["name"]
    if error is not None:
        logger.bind(tag=TAG).warning(f"{name}标记解析失败：{error}，来源={source}，设备={device_key}")
        announce(device_key, domain["failure_text"](None, error), bot_id=domain["bot_id"])
        return
    logger.bind(tag=TAG).info(
        f"{name}标记命中：来源={source}，action={spec.get('action')}，设备={device_key}")
    if device_key:
        try:
            # 按名从模块全局取执行函数：测试与外部可替换 execute/execute_timer
            runner = globals()[domain["execute_name"]]
            asyncio.ensure_future(runner(device_key, spec))
        except RuntimeError:
            logger.bind(tag=TAG).warning(f"{name}桥没有可用的事件循环，跳过执行")
            announce(device_key, domain["failure_text"](spec, "设备还没准备好"), bot_id=domain["bot_id"])


def handle_marker_line(device_key, marker_line, source="增量"):
    """一行已确认的标记（首行前缀门交出）：按行首的标记分发到对应域并安排执行。

    返回 True 表示这一行确实是某个域的标记——合法的安排执行，非法的走失败
    补播报；不以任何域的标记开头则返回 False、零副作用。
    """
    for domain in DOMAINS:
        if marker_line.startswith(domain["marker"]):
            _, spec, error = domain["extract"](marker_line)
            if spec is None and error is None:
                return False
            _dispatch(device_key, domain, spec, error, source)
            return True
    return False


class FirstLineGate:
    """回合首行前缀门（纯逻辑）：send() 放行首段文本前先判定是不是标记行。

    把增量逐段喂进来，只攒到「已到达文本不再可能是任一标记的前缀」为止：
    累积文本与全部标记都在某个字符分叉（普通回复通常第一个字就分叉，如
    「好的…」）即判普通，缓冲原样放行、之后直通，普通回复的额外延迟约一个
    字；以「【」开头但第二字分叉（如【播报】类）同样立即放行——【闹钟】与
    【倒计时】共享的公共前缀只有一个「【」字，前缀判定按标记集合推进，
    不会把任何一个域误放行或误吞。确认首行以某个完整标记开头后继续攒到
    首行结束（换行），整行作为标记行交出；攒过 MAX_BUFFER 仍定不下来按
    普通文本放行，防死等。feed() 返回 (放行文本, 标记行或 None)；finish()
    在流结束时对残留缓冲收尾——恰好是一行标记则交出它，残缺的标记前缀
    原样放行。
    """

    MAX_BUFFER = 300

    def __init__(self, markers=None):
        self._markers = tuple(markers) if markers else ALL_MARKERS
        self._buf = ""
        self.decided = False

    def feed(self, text):
        if self.decided:
            return text or "", None
        self._buf += text or ""
        return self._decide(final=False)

    def finish(self):
        if self.decided:
            return "", None
        return self._decide(final=True)

    def _release(self, text):
        self.decided = True
        self._buf = ""
        return text, None

    def _decide(self, final):
        buf = self._buf
        pos = 0
        while True:  # 跳过前置空行，与 extract 的容忍口径一致
            nl = buf.find("\n", pos)
            line = buf[pos:] if nl < 0 else buf[pos:nl]
            if line.strip():
                break
            if nl < 0:  # 到目前为止只有空白
                if final or len(buf) > self.MAX_BUFFER:
                    return self._release(buf)
                return "", None
            pos = nl + 1
        probe = buf[pos:].lstrip()
        nl = probe.find("\n")
        first_line = probe if nl < 0 else probe[:nl]
        confirmed = None
        pending = False
        for marker in self._markers:
            if first_line.startswith(marker):
                confirmed = marker
                break
            if marker.startswith(first_line):
                pending = True
        if confirmed is None and not pending:
            return self._release(buf)  # 与全部标记都在某个字符分叉：普通文本
        if confirmed is None:
            # 首行还是某个标记的真前缀：行已结束（或流结束）就不是标记了
            if nl >= 0 or final or len(buf) > self.MAX_BUFFER:
                return self._release(buf)
            return "", None
        # 首行以某个完整标记开头
        if nl >= 0:
            self.decided = True
            self._buf = ""
            return probe[nl + 1:], first_line.strip()
        if final:  # 整段就是一行标记
            self.decided = True
            self._buf = ""
            return "", first_line.strip()
        if len(buf) > self.MAX_BUFFER:
            return self._release(buf)
        return "", None


def process_first_text(device_key, text, streamed=None):
    """处理回合回复的第一段文本。返回 (要继续往下发的文本, 是否命中了标记)。

    两个来源都查标记（与常驻监听 watcher 同款做法），每个来源依次试两个
    域：最终文本（reply.text，即 display_text/transcript）会丢开头的标记
    行、增量累积（reply.streamed）里却完整（反之亦可能）。最终文本命中时
    以它为准（剥掉标记行再往下发）；最终文本没有、增量里有时，判定与执行
    参数取增量的，下发文本里与标记行对应的那行一并剥掉（最终文本的渲染
    可能把标记行改了模样，如丢掉前缀只剩 JSON——见 strip_marker_from_final）；
    最终文本已把整行丢掉时不动它。两边都有也只执行一次。

    没标记时原样返回、零副作用；解析失败则安排一句失败补播报——任何情况
    都不会让回合崩溃。
    """
    for domain in DOMAINS:
        stripped, spec, error = domain["extract"](text)
        if spec is not None or error is not None:
            _dispatch(device_key, domain, spec, error, "最终文本")
            return stripped, True
    if streamed:
        for domain in DOMAINS:
            _, spec, error = domain["extract"](streamed)
            if spec is not None or error is not None:
                stripped = strip_marker_from_final(text, _first_content_line(streamed))
                _dispatch(device_key, domain, spec, error, "增量")
                return stripped, True
    return text, False

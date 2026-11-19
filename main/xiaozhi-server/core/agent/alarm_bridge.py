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
    """把一句结果放进待汇报箱（可从任何线程/循环调用，失败只记日志）。

    桥的结果只念不记：record=False 投递——不写进设备对话、聊天记录里不留，
    设备端也不上屏（见 reports.speak 的只念不记发声）。"""
    if not device_key or not text:
        return
    try:
        from core.agent import reports

        reports.add(device_key, BOT_ID, text, record=False)
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

def _first_content_line(text):
    """文本里第一个非空行（剥过首尾空白）；没有返回空串。"""
    for line in (text or "").split("\n"):
        if line.strip():
            return line.strip()
    return ""


def strip_marker_from_final(text, marker_line):
    """从最终文本里剥掉与标记行对应的第一行；没有对应行时原样返回。

    最终文本（display_text）对标记行的渲染与增量原文不总是一致：可能原样
    保留，也可能丢掉【闹钟】前缀、只剩 JSON 那一行（2026-11-19 08:35 实测：
    桥从增量命中、最终文本原样下发，JSON 行就这么漏进 TTS 和对话列表）。
    两种形态出现在首个非空行时都剥掉；首个非空行是别的内容，说明最终文本
    已把标记行整行丢掉、正文就是首行，不动它。
    """
    if not text or not marker_line:
        return text
    payload = marker_line[len(MARKER):].strip() if marker_line.startswith(MARKER) else ""
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


def _dispatch(device_key, spec, error, source):
    """命中标记后的统一出口：记日志，安排执行或失败补播报。"""
    if error is not None:
        logger.bind(tag=TAG).warning(f"闹钟标记解析失败：{error}，来源={source}，设备={device_key}")
        announce(device_key, failure_text(None, error))
        return
    logger.bind(tag=TAG).info(
        f"闹钟标记命中：来源={source}，action={spec.get('action')}，设备={device_key}")
    if device_key:
        try:
            asyncio.ensure_future(execute(device_key, spec))
        except RuntimeError:
            logger.bind(tag=TAG).warning("闹钟桥没有可用的事件循环，跳过执行")
            announce(device_key, failure_text(spec, "设备还没准备好"))


def handle_marker_line(device_key, marker_line, source="增量"):
    """一行已确认的闹钟标记（首行前缀门交出）：解析并安排执行/失败补播报。

    返回 True 表示这一行确实是标记——合法的安排执行，非法的走失败补播报；
    不是标记则返回 False、零副作用。
    """
    _, spec, error = extract(marker_line)
    if spec is None and error is None:
        return False
    _dispatch(device_key, spec, error, source)
    return True


class FirstLineGate:
    """回合首行前缀门（纯逻辑）：send() 放行首段文本前先判定是不是闹钟标记。

    把增量逐段喂进来，只攒到「已到达文本不再可能是标记前缀」为止：累积
    文本与 MARKER 在某个字符分叉（普通回复通常第一个字就分叉，如「好的…」）
    即判普通，缓冲原样放行、之后直通，普通回复的额外延迟约一个字；以
    「【」开头但第二字分叉（如【播报】类）同样立即放行。确认以 MARKER
    开头后继续攒到首行结束（换行），整行作为标记行交出；攒过 MAX_BUFFER
    仍定不下来按普通文本放行，防死等。feed() 返回 (放行文本, 标记行或
    None)；finish() 在流结束时对残留缓冲收尾——恰好是一行标记则交出它，
    残缺的标记前缀原样放行。
    """

    MAX_BUFFER = 300

    def __init__(self):
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
        n = 0
        while n < len(first_line) and n < len(MARKER) and first_line[n] == MARKER[n]:
            n += 1
        if n < len(first_line) and n < len(MARKER):
            return self._release(buf)  # 第 n 个字符与标记分叉：普通文本
        if len(first_line) < len(MARKER):
            # 首行还是 MARKER 的真前缀：行已结束（或流结束）就不是标记了
            if nl >= 0 or final or len(buf) > self.MAX_BUFFER:
                return self._release(buf)
            return "", None
        # 首行以完整 MARKER 开头
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

    两个来源都查标记（与常驻监听 watcher 同款做法）：最终文本（reply.text，
    即 display_text/transcript）会丢开头的标记行、增量累积（reply.streamed）
    里却完整（反之亦可能）。最终文本命中时以它为准（剥掉标记行再往下发）；
    最终文本没有、增量里有时，判定与执行参数取增量的，下发文本里与标记行
    对应的那行一并剥掉（最终文本的渲染可能把标记行改了模样，如丢掉前缀
    只剩 JSON——见 strip_marker_from_final）；最终文本已把整行丢掉时不动它。
    两边都有也只执行一次。

    没标记时原样返回、零副作用；解析失败则安排一句失败补播报——任何情况
    都不会让回合崩溃。
    """
    stripped, spec, error = extract(text)
    source = "最终文本"
    if spec is None and error is None and streamed:
        alt_stripped, alt_spec, alt_error = extract(streamed)
        if alt_spec is not None or alt_error is not None:
            spec, error = alt_spec, alt_error
            source = "增量"
            stripped = strip_marker_from_final(text, _first_content_line(streamed))
    if spec is None and error is None:
        return text, False
    _dispatch(device_key, spec, error, source)
    return stripped, True

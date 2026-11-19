"""待汇报箱：在外面干活的 Agent（目前只有 Grok Bot）做完后要念给用户的话。

设备被打断或等超时后，Grok Bot 在云端继续做；做完的结果先放进这里，再找设备空闲的时候念：
- 设备在线且空闲（没在说话、没在听你说、不在一轮对话里）：马上念；
- 设备不在线：留着，下次连上后等问候播完再念；
- 用户说"嗨 孙悟空"切回去、或给它派新活：先念它的待汇报。

带屏幕的设备只在对话前后才连着，待汇报会一直等到下次唤醒。本机设备接口（/kiosk/reports）让它随时查有几条，
用户点一下就连上来念（deliver_now）；设备在睡眠时段时标成"安静"（set_quiet），连着也不主动念，等用户点或唤醒。

只放内存，服务重启就丢（用户决定）；24 小时没念掉的也丢。
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config.logger import setup_logging
from core.agent.grokbot_client import _sleep

TAG = __name__
logger = setup_logging()

EXPIRE_SECONDS = 24 * 3600
MAX_PER_DEVICE = 20
DELIVER_TIMEOUT = 600  # 设备一直不空闲时最多等这么久，之后留到下次机会
# 设备刚连上时先等唤醒问候播完再念。问候那段没有"正在说话"的标志（它不经过 STT 消息），只能靠时间错开
AFTER_CONNECT_DELAY = 6


@dataclass
class Report:
    bot_id: str
    text: str
    at: float = field(default_factory=time.time)
    record: bool = True  # False = 只念不记：不写进对话/聊天记录，设备端也不上屏（闹钟桥结果用）


_lock = threading.Lock()
_box: Dict[str, List[Report]] = {}  # 设备 -> 待汇报
_delivering = set()  # 正在等设备空闲的设备
_quiet = set()  # 睡眠时段的设备：有待汇报也不主动念


def device_key(conn) -> str:
    return getattr(conn, "device_id", None) or conn.session_id


def _prune(items: List[Report]) -> List[Report]:
    now = time.time()
    return [r for r in items if now - r.at < EXPIRE_SECONDS][-MAX_PER_DEVICE:]


def add(key: str, bot_id: str, text: str, record: bool = True) -> None:
    """放一条待汇报，设备在线就安排念。可以从任何线程调用。

    record=False 的条目只念不记（见 Report.record），投递时单独成段发声。"""
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        _box[key] = _prune(_box.get(key, [])) + [Report(bot_id, text, record=record)]
    logger.bind(tag=TAG).info(f"设备 {key} 新增待汇报：{text[:60]}")
    _try_deliver(key)


def pending(key: str, bot_id: str = None) -> bool:
    with _lock:
        items = _prune(_box.get(key, []))
        _box[key] = items
    return any(bot_id is None or r.bot_id == bot_id for r in items)


def count(key: str) -> int:
    with _lock:
        items = _prune(_box.get(key, []))
        _box[key] = items
    return len(items)


def set_quiet(key: str, quiet: bool) -> None:
    """设备进入或离开睡眠时段。只放内存，设备每次查询都会重新告诉一遍，服务重启不影响。"""
    with _lock:
        if quiet:
            _quiet.add(key)
        else:
            _quiet.discard(key)


def take(key: str, bot_id: str = None) -> List[Report]:
    """取走（并删除）这台设备的待汇报；指定 bot_id 时只取这个 Bot 的。"""
    with _lock:
        items = _prune(_box.get(key, []))
        picked = [r for r in items if bot_id is None or r.bot_id == bot_id]
        _box[key] = [r for r in items if r not in picked]
    return picked


def take_text(key: str, bot_id: str = None) -> str:
    return "".join(r.text for r in take(key, bot_id))


def clear() -> None:
    with _lock:
        _box.clear()
        _delivering.clear()
        _quiet.clear()


# ---------- 找机会念 ----------

def _find_conn(key: str):
    from core.admin import connections

    for item in connections.snapshot():
        conn = connections.get(item["session_id"])
        if conn is not None and device_key(conn) == key:
            return conn
    return None


def _schedule(key: str) -> bool:
    """设备在线就安排等它空闲时念；不在线返回 False。"""
    conn = _find_conn(key)
    loop = getattr(conn, "loop", None) if conn is not None else None
    if conn is None or loop is None:
        return False
    import asyncio

    asyncio.run_coroutine_threadsafe(deliver_when_idle(conn), loop)
    return True


def _try_deliver(key: str) -> None:
    with _lock:
        if key in _quiet:
            return
    _schedule(key)


def deliver_now(key: str) -> bool:
    """用户要听（点了"有一条待汇报"）：睡眠时段也念。设备没连着返回 False。"""
    return _schedule(key)


def is_idle(conn) -> bool:
    """设备没在说话（含等回复）、没在听人说，TTS 也就绪了。"""
    return (not getattr(conn, "client_is_speaking", False) and not getattr(conn, "client_have_voice", False)
            and getattr(conn, "tts", None) is not None)


def still_online(conn) -> bool:
    from core.admin import connections

    return connections.get(getattr(conn, "session_id", None)) is conn


async def deliver_when_idle(conn, initial_delay: float = 0, timeout: float = DELIVER_TIMEOUT) -> bool:
    """等设备空闲后把待汇报念出来。同一台设备同时只有一个在等。返回是否念了。"""
    key = device_key(conn)
    with _lock:
        if key in _delivering:
            return False
        _delivering.add(key)
    try:
        await _sleep(initial_delay)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not still_online(conn) or not pending(key):
                return False
            if is_idle(conn):
                picked = take(key)
                if not picked:
                    return False
                try:
                    # 相邻且 record 一致的合并成一段念；只念不记的段落单独发声，
                    # 免得和要入对话的待汇报混进同一句。
                    segments = []
                    for r in picked:
                        if segments and segments[-1][1] == r.record:
                            segments[-1][0] += r.text
                        else:
                            segments.append([r.text, r.record])
                    for seg_text, seg_record in segments:
                        speak(conn, seg_text, record=seg_record)
                except Exception as e:  # noqa: BLE001 —— 没念成就放回去，留到下次
                    logger.bind(tag=TAG).warning(f"播报待汇报失败，放回去：{e!r}")
                    with _lock:
                        _box[key] = _prune(picked + _box.get(key, []))
                    return False
                return True
            await _sleep(0.5)
        logger.bind(tag=TAG).info(f"设备 {key} 一直不空闲，待汇报留到下次")
        return False
    finally:
        with _lock:
            _delivering.discard(key)


def speak(conn, text: str, record: bool = True) -> None:
    """服务端主动说一句（和呼名回复"我在"走同一条路）。

    record=False 时只念不记：不经 speak_txt（它会把文本写进对话），改走
    clear_command 同款的只发声队列；同时把这一句的 sentence_id 登记到
    conn.announce_sentence_ids，TTS 下发时据此给句子打 announce 标记，
    设备端收到后只出声、不上屏、不进本地聊天记录。"""
    conn.client_abort = False
    conn.sentence_id = str(uuid.uuid4().hex)
    conn.client_is_speaking = True  # 和一轮对话一样：播完 TTS 会清掉；期间再来的待汇报等着，不会截断这条
    conn.logger.bind(tag=TAG).info(f"播报待汇报：{text[:80]}")
    if record:
        from core.handle.intentHandler import speak_txt

        speak_txt(conn, text)
        return
    ids = getattr(conn, "announce_sentence_ids", None)
    if ids is None:
        ids = conn.announce_sentence_ids = set()
    ids.add(conn.sentence_id)
    _speak_only(conn, text)


def _speak_only(conn, text: str) -> None:
    """只发声不写对话：与 clear_command._speak 同款队列法，唯独不 put 进 dialogue。"""
    from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO

    conn.tts.store_tts_text(conn.sentence_id, text)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )


def on_conn_open(conn) -> None:
    """设备连上时：有待汇报就等问候播完再念。"""
    key = device_key(conn)
    loop = getattr(conn, "loop", None)
    if loop is None or not pending(key):
        return
    import asyncio

    asyncio.run_coroutine_threadsafe(deliver_when_idle(conn, initial_delay=AFTER_CONNECT_DELAY), loop)

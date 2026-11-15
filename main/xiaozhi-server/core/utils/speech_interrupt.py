"""协商回答期间的语音用途；停止词是独立指令，不作为新问题交给模型。"""

import re

_PUNCTUATION = re.compile(r"[\W_]+", re.UNICODE)
STOP_WORDS = frozenset({"停止", "暂停"})


def mode(conn):
    value = (getattr(conn, "features", None) or {}).get("speech_interrupt", "any")
    return value if value in ("keywords", "off", "any") else "any"


def restricted(conn):
    return mode(conn) != "any" and conn.client_listen_mode != "manual"


def active(conn):
    return restricted(conn) and (
        getattr(conn, "interrupt_only", False) or conn.client_is_speaking
    )


def mark_reply(conn):
    if restricted(conn):
        # 由设备实际播放完后的 listen/start 解除，覆盖设备端播放缓冲。
        conn.interrupt_only = True


def action(conn, text, captured_during_reply):
    if not restricted(conn) or not captured_during_reply:
        return "chat"
    # 上一句的识别迟到时，不能把已恢复的正常收音再打断。
    if not active(conn):
        return "ignore"
    normalized = _PUNCTUATION.sub("", str(text or ""))
    return "stop" if mode(conn) == "keywords" and normalized in STOP_WORDS else "ignore"

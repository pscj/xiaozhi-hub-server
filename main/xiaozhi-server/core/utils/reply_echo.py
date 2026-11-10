"""回声识别过滤：丢掉把设备自己的回复识别成"用户说话"的结果。

开着回声消除的设备（hello 里带 aec）在回复播放时也开着麦克风（realtime 模式），方便用户随时插话。
残留的回声很轻，但语音识别有时仍会把它认成回复里的一小段（例如"也是。"），被当成用户插话，把回复打断。

做法：回复播放期间和播完后几秒内，识别结果里大部分字都出现在这次回复里，就当回声丢掉。
若识别把较长的回复前缀和独立的用户追问拼成一句，先去掉明确匹配的回声前缀，再检查余下的追问。
只看字，不看声音大小：回声的音量和用户离设备远近、音量设置都有关，定不出一个可靠的门限。
"""

import re
import time
from difflib import SequenceMatcher

import numpy as np

# 最后一段回复音频发出后还算"回复期间"的秒数：设备还要把缓冲播完，回声约 0.4 秒后到麦克风，
# VAD 又要等 0.8 秒安静才把这句交给识别
WINDOW_AFTER = 4.0
MIN_SHARE = 0.6  # 识别出的字里，至少这么大比例也出现在回复里，才算回声
_NON_WORD = re.compile(r"[\W_]+")


def _state(conn) -> dict:
    st = getattr(conn, "reply_echo", None)
    if st is None:
        st = {"sid": None, "text": "", "at": 0.0}
        conn.reply_echo = st
    return st


def note_reply(conn, text) -> None:
    """一句回复正在发给设备（text 为 None 表示这轮的音频发完了，只更新时间）。"""
    st = _state(conn)
    if st["sid"] != conn.sentence_id:  # 新的一轮回复：忘掉上一轮的
        st.update(sid=conn.sentence_id, text="")
    if text:
        st["text"] += _NON_WORD.sub("", str(text))
    st["at"] = time.monotonic()


def during_reply(conn, context=None) -> bool:
    if context is not None:
        return bool(context["active"] and context["text"])
    st = _state(conn)
    return bool(st["text"]) and (conn.client_is_speaking or time.monotonic() - st["at"] <= WINDOW_AFTER)


def capture_context(conn) -> dict:
    """把首个有声帧时的回声上下文绑定到本句，避免收音/识别耗时使其过期。"""
    return {"text": _state(conn)["text"], "active": during_reply(conn)}


def is_echo(conn, text, context=None) -> bool:
    """回复期间识别出的这句话是不是回复自己的回声。"""
    if not during_reply(conn, context):
        return False
    heard = _NON_WORD.sub("", str(text))
    if not heard:
        return False
    reply = context["text"] if context is not None else _state(conn)["text"]
    blocks = SequenceMatcher(None, heard, reply, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / len(heard) >= MIN_SHARE


def strip_reply_prefix(conn, text, context=None):
    """ASR 把回复回声和追问拼成一句时，只去掉可明确匹配的回声前缀。

    精确前缀至少六个字且有标点/空格分界；完整回复尾部至少七个字时
    兼容一次同长度 ASR 替换。余下至少两个字，且不能仍是回复回声。
    纯回声仍交给 is_echo，避免把回复尾部拆成新的用户问题。
    """
    if not isinstance(text, str) or not during_reply(conn, context):
        return text
    positions = [i for i, char in enumerate(text) if not _NON_WORD.fullmatch(char)]
    heard = "".join(text[i] for i in positions)
    reply = context["text"] if context is not None else _state(conn)["text"]
    for size in range(min(len(heard) - 2, len(reply)), 5, -1):
        exact = heard[:size] in reply
        # ASR 可把“您”识别成“你”：仅允许完整回复尾部的一处同长度替换。
        # 不以宽泛的字数占比截断用户原话。
        one_substitution = (size >= 7 and size <= len(reply)
                            and sum(a != b for a, b in zip(heard[:size], reply[-size:])) == 1)
        if not exact and not one_substitution:
            continue
        # Require an ASR sentence/word boundary, not just shared characters.
        end = positions[size - 1] + 1
        if positions[size] == end and not one_substitution:
            continue
        remainder = text[positions[size]:].strip()
        if not is_echo(conn, remainder, context):
            return remainder
    return text


def level_db(frames) -> float:
    """这句话里最响的 60ms 的音量（16kHz PCM 帧），单位 dBFS。只用于日志，方便调参时对照。"""
    pcm = np.frombuffer(b"".join(frames), dtype="<i2").astype(np.float32) / 32768
    if not len(pcm):
        return -120.0
    step = 960
    rms = [np.sqrt(np.mean(pcm[i:i + step] ** 2)) for i in range(0, len(pcm), step)]
    return float(20 * np.log10(max(max(rms), 1e-6)))

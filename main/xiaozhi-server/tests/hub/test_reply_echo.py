"""[xiaozhi-hub] 回声识别过滤：回复播放期间和播完后几秒内，识别结果大部分是回复自己的字就丢掉。"""

import struct
from types import SimpleNamespace

from core.utils import reply_echo


def make_conn():
    return SimpleNamespace(sentence_id="s1", client_is_speaking=True)


def test_echo_of_reply_is_dropped_while_speaking():
    conn = make_conn()
    assert not reply_echo.during_reply(conn) and not reply_echo.is_echo(conn, "也是。")  # 还没有回复
    reply_echo.note_reply(conn, "我觉得也是，")
    reply_echo.note_reply(conn, "明天会下雨。")
    assert reply_echo.during_reply(conn)
    assert reply_echo.is_echo(conn, "也是。")
    assert reply_echo.is_echo(conn, "明天会下鱼")  # 认错一个字也算
    assert not reply_echo.is_echo(conn, "停一下，帮我查查股票")  # 用户自己的话
    assert not reply_echo.is_echo(conn, "。")


def test_window_after_reply_and_new_reply_resets(monkeypatch):
    conn = make_conn()
    now = [100.0]
    monkeypatch.setattr(reply_echo.time, "monotonic", lambda: now[0])
    reply_echo.note_reply(conn, "今天天气不错。")
    reply_echo.note_reply(conn, None)  # 这轮的音频发完了
    conn.client_is_speaking = False
    now[0] += reply_echo.WINDOW_AFTER - 0.5
    assert reply_echo.is_echo(conn, "天气不错")  # 设备还在播缓冲，回声还会来
    now[0] += 1
    assert not reply_echo.during_reply(conn) and not reply_echo.is_echo(conn, "天气不错")  # 过了窗口算用户说的
    # 新的一轮回复：上一轮的字不再算
    conn.sentence_id, conn.client_is_speaking = "s2", True
    reply_echo.note_reply(conn, "好的。")
    assert not reply_echo.is_echo(conn, "天气不错") and reply_echo.is_echo(conn, "好的")


def test_level_db():
    loud = struct.pack("<960h", *([16384] * 960))  # 半幅方波 ≈ -6 dBFS
    quiet = struct.pack("<960h", *([0] * 960))
    assert round(reply_echo.level_db([quiet, loud])) == -6
    assert reply_echo.level_db([]) == -120.0


def test_greeting_echo_prefix_keeps_weather_question():
    for reply, heard in [
        ("请问您需要什么帮助？", "请问您需要什么帮助？ 今天的天气"),
        ("我在这里，等候您的指令。", "我在这里等候您的指令， 今天的天气"),
    ]:
        conn = make_conn()
        reply_echo.note_reply(conn, reply)
        assert reply_echo.is_echo(conn, heard)  # 旧路径会误丢整句
        cleaned = reply_echo.strip_reply_prefix(conn, heard)
        assert cleaned == "今天的天气"
        assert not reply_echo.is_echo(conn, cleaned)


def test_prefix_trimming_does_not_turn_pure_echo_into_a_question():
    conn = make_conn()
    reply_echo.note_reply(conn, "请问您需要什么帮助？今天的天气晴朗。")
    for heard in ("请问您需要什么帮助？", "请问您需要什么帮助？今天的天气晴朗。",
                  "请问您需要什么帮助？今天的天汽晴朗。", "今天天气怎么样？"):
        assert reply_echo.strip_reply_prefix(conn, heard) == heard


def test_prefix_trimming_requires_long_match_and_boundary():
    conn = make_conn()
    reply_echo.note_reply(conn, "我在这里，等候您的指令。")
    for heard in ("我在这里，今天的天气", "我在这里等候您的指令是什么意思？"):
        assert reply_echo.strip_reply_prefix(conn, heard) == heard
    conn.client_is_speaking = False
    conn.reply_echo["at"] -= reply_echo.WINDOW_AFTER + 1
    heard = "我在这里等候您的指令， 今天的天气"
    assert reply_echo.strip_reply_prefix(conn, heard) == heard


def test_screenshot_prefix_with_one_asr_substitution():
    conn = make_conn()
    reply_echo.note_reply(conn, "我一直都在呢，您请说。")
    assert reply_echo.strip_reply_prefix(conn, "一直都在呢， 你请说你查到了什么") == "你查到了什么"
    assert reply_echo.strip_reply_prefix(conn, "一直都在呢， 你请说") == "一直都在呢， 你请说"


def test_context_survives_long_capture_and_recognition(monkeypatch):
    conn = make_conn()
    now = [100.0]
    monkeypatch.setattr(reply_echo.time, "monotonic", lambda: now[0])
    reply_echo.note_reply(conn, "我认真听着呢，请讲。")
    conn.client_is_speaking = False
    context = reply_echo.capture_context(conn)
    now[0] += 10
    heard = "我认真听着呢， 请讲， 刚才资料查得怎么样了？"
    assert not reply_echo.during_reply(conn)
    assert reply_echo.strip_reply_prefix(conn, heard, context) == "刚才资料查得怎么样了？"
    assert reply_echo.is_echo(conn, "我认真听着呢，请讲。", context)
    # 后来的回复不能改变已经开始识别的上一句上下文。
    conn.sentence_id = "next"
    reply_echo.note_reply(conn, "这是下一轮回复。")
    assert reply_echo.strip_reply_prefix(conn, heard, context) == "刚才资料查得怎么样了？"


def test_inactive_capture_does_not_gain_an_echo_window_later():
    conn = make_conn()
    context = reply_echo.capture_context(conn)
    reply_echo.note_reply(conn, "我认真听着呢，请讲。")
    heard = "我认真听着呢，请讲，刚才资料查得怎么样了？"
    assert reply_echo.strip_reply_prefix(conn, heard, context) == heard

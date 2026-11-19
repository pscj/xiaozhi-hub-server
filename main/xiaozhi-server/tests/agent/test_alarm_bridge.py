"""Muse 通道闹钟桥：标记提取、动作映射、结果文案、执行投递（工具调用与待汇报箱都 mock）。"""

import asyncio
import json
from datetime import datetime

import pytest

from core.agent import alarm_bridge, reports
from core.providers.tools.device_mcp import mcp_handler

NOW = datetime(2026, 11, 19, 8, 0)


@pytest.fixture(autouse=True)
def real_async_sleep(monkeypatch):
    # 同 test_muse_proactive：全局 conftest 的 HTTP mock 把 asyncio.sleep 替成了
    # 不让出循环的函数，涉及任务调度的断言必须用真实调度。
    monkeypatch.setattr(asyncio, "sleep", asyncio.tasks.sleep)


# ---------- 标记提取 ----------

def test_extract_no_marker():
    text = "好的，明天七点半我叫你起床。"
    stripped, spec, error = alarm_bridge.extract(text)
    assert stripped == text and spec is None and error is None


def test_extract_marker_first_line():
    text = '【闹钟】{"action":"create","time":"07:30","repeat":"daily","label":"起床"}\n好的，已经记下了。'
    stripped, spec, error = alarm_bridge.extract(text)
    assert error is None
    assert spec == {"action": "create", "time": "07:30", "repeat": "daily", "label": "起床"}
    assert stripped == "好的，已经记下了。"


def test_extract_marker_only_line():
    stripped, spec, error = alarm_bridge.extract('【闹钟】{"action":"list"}')
    assert error is None and spec == {"action": "list"} and stripped == ""


def test_extract_marker_with_extra_whitespace():
    text = '\n\n  【闹钟】  {"action":"list"}  \n\n你现在没有闹钟。'
    stripped, spec, error = alarm_bridge.extract(text)
    assert error is None and spec == {"action": "list"}
    assert stripped == "你现在没有闹钟。"


def test_extract_marker_not_on_first_line_does_not_trigger():
    text = '好的。\n【闹钟】{"action":"list"}'
    stripped, spec, error = alarm_bridge.extract(text)
    assert stripped == text and spec is None and error is None


def test_extract_invalid_json_still_strips_and_errors():
    stripped, spec, error = alarm_bridge.extract("【闹钟】{这不是JSON}\n好的。")
    assert spec is None and error and "JSON" in error
    assert stripped == "好的。"


def test_extract_unknown_action():
    _, spec, error = alarm_bridge.extract('【闹钟】{"action":"explode"}')
    assert spec is None and error and "explode" in error


def test_extract_missing_fields():
    _, spec, error = alarm_bridge.extract('【闹钟】{"action":"create"}')
    assert spec is None and error == "缺少时间"
    _, spec, error = alarm_bridge.extract('【闹钟】{"action":"cancel"}')
    assert spec is None and error == "缺少闹钟编号"
    _, spec, error = alarm_bridge.extract('【闹钟】{"action":"set_enabled","id":"a1"}')
    assert spec is None and error == "缺少开关状态"


def test_extract_set_enabled_string_bool_coerced():
    _, spec, error = alarm_bridge.extract('【闹钟】{"action":"set_enabled","id":"a1","enabled":"false"}')
    assert error is None and spec["enabled"] is False


# ---------- 动作映射与参数 ----------

def test_action_tool_mapping():
    assert alarm_bridge.ACTION_TOOLS == {
        "create": "self.alarm.create",
        "list": "self.alarm.list",
        "cancel": "self.alarm.cancel",
        "set_enabled": "self.alarm.set_enabled",
    }


def test_build_args():
    assert alarm_bridge.build_args({"action": "create", "time": "07:30"}) == {"time": "07:30"}
    assert alarm_bridge.build_args(
        {"action": "create", "time": "07:30", "repeat": "weekly", "weekdays": [1, 3], "label": "开会"}
    ) == {"time": "07:30", "repeat": "weekly", "weekdays": [1, 3], "label": "开会"}
    assert alarm_bridge.build_args({"action": "list"}) == {}
    assert alarm_bridge.build_args({"action": "cancel", "id": "a1"}) == {"id": "a1"}
    assert alarm_bridge.build_args({"action": "set_enabled", "id": "a1", "enabled": True}) == {
        "id": "a1", "enabled": True,
    }


# ---------- 结果文案 ----------

def test_success_text_create():
    spec = {"action": "create", "time": "07:30", "repeat": "daily", "label": "起床"}
    data = {"success": True, "id": "a1", "next_fire": "2026-11-20T07:30:00"}
    assert alarm_bridge.success_text(spec, data, now=NOW) == "闹钟已设好，起床，7点30分，每天，下一次是明天7点30分。"


def test_success_text_create_once_no_label():
    spec = {"action": "create", "time": "08:00"}
    data = {"success": True, "id": "a2", "next_fire": "2026-11-19T08:00:00"}
    assert alarm_bridge.success_text(spec, data, now=NOW) == "闹钟已设好，8点，下一次是今天8点。"


def test_success_text_list_empty():
    assert alarm_bridge.success_text({"action": "list"}, {"success": True, "alarms": []}) == "你现在没有设闹钟。"


def test_success_text_list_multiple():
    data = {"success": True, "alarms": [
        {"id": "a1", "label": "起床", "time": "07:30", "repeat": {"type": "daily"}, "enabled": True},
        {"id": "a2", "label": "开会", "time": "09:00", "repeat": {"type": "weekly", "weekdays": [1, 3, 5]}, "enabled": False},
        {"id": "a3", "label": "", "time": "22:15", "repeat": {"type": "once"}, "enabled": True},
    ]}
    assert alarm_bridge.success_text({"action": "list"}, data) == (
        "你一共有3个闹钟：起床，7点30分，每天；开会，9点，每周一、周三、周五，已关闭；闹钟，22点15分。"
    )


def test_success_text_cancel_and_set_enabled():
    assert alarm_bridge.success_text({"action": "cancel", "id": "a1"}, {"success": True}) == "闹钟已经取消了。"
    spec = {"action": "set_enabled", "id": "a1", "enabled": True}
    data = {"success": True, "next_fire": "2026-11-21T07:30:00"}
    assert alarm_bridge.success_text(spec, data, now=NOW) == "闹钟已打开，下一次是后天7点30分。"
    spec = {"action": "set_enabled", "id": "a1", "enabled": False}
    assert alarm_bridge.success_text(spec, {"success": True}) == "闹钟已关闭。"


def test_failure_text_prefix_per_action():
    assert alarm_bridge.failure_text({"action": "create"}, "时间不对") == "闹钟没设成：时间不对。"
    assert alarm_bridge.failure_text({"action": "list"}, "设备不在线") == "闹钟列表没查成：设备不在线。"
    assert alarm_bridge.failure_text(None, "格式不对") == "闹钟没设成：格式不对。"


# ---------- 执行（mock 工具调用与待汇报箱） ----------

class FakeConn:
    def __init__(self, loop):
        self.session_id, self.device_id = "s-1", "dev-1"
        self.loop = loop
        self.mcp_client = object()


@pytest.fixture
def announced(monkeypatch):
    out = []
    monkeypatch.setattr(reports, "add", lambda key, bot_id, text, record=True: out.append((key, bot_id, text)))
    return out


async def test_execute_create_calls_sanitized_tool_and_announces(monkeypatch, announced):
    conn = FakeConn(asyncio.get_running_loop())
    monkeypatch.setattr(alarm_bridge, "_find_conn", lambda key: conn)
    calls = []

    async def fake_call(conn_, client, tool_name, args, timeout=30):
        calls.append((tool_name, json.loads(args)))
        return json.dumps({"success": True, "id": "a1", "next_fire": "2026-11-20T07:30:00"})

    monkeypatch.setattr(mcp_handler, "call_mcp_tool", fake_call)
    spec = {"action": "create", "time": "07:30", "repeat": "daily", "label": "起床"}
    await alarm_bridge.execute("dev-1", spec)
    assert calls == [("self_alarm_create", {"time": "07:30", "repeat": "daily", "label": "起床"})]
    assert len(announced) == 1
    key, bot_id, text = announced[0]
    assert (key, bot_id) == ("dev-1", "alarm")
    assert text.startswith("闹钟已设好，起床，7点30分，每天，下一次是") and "7点30分。" in text


async def test_execute_device_offline(monkeypatch, announced):
    monkeypatch.setattr(alarm_bridge, "_find_conn", lambda key: None)
    await alarm_bridge.execute("dev-1", {"action": "create", "time": "07:30"})
    assert announced == [("dev-1", "alarm", "闹钟没设成：设备不在线。")]


async def test_execute_tool_missing_announces_failure(monkeypatch, announced):
    conn = FakeConn(asyncio.get_running_loop())
    monkeypatch.setattr(alarm_bridge, "_find_conn", lambda key: conn)

    async def fake_call(conn_, client, tool_name, args, timeout=30):
        raise ValueError(f"工具 {tool_name} 不存在")

    monkeypatch.setattr(mcp_handler, "call_mcp_tool", fake_call)
    await alarm_bridge.execute("dev-1", {"action": "list"})
    assert announced == [("dev-1", "alarm", "闹钟列表没查成：设备上还没有闹钟功能。")]


async def test_execute_device_error_result_announces_failure(monkeypatch, announced):
    conn = FakeConn(asyncio.get_running_loop())
    monkeypatch.setattr(alarm_bridge, "_find_conn", lambda key: conn)

    async def fake_call(conn_, client, tool_name, args, timeout=30):
        return json.dumps({"success": False, "error": "时间格式不对"})

    monkeypatch.setattr(mcp_handler, "call_mcp_tool", fake_call)
    await alarm_bridge.execute("dev-1", {"action": "create", "time": "07:30"})
    assert announced == [("dev-1", "alarm", "闹钟没设成：时间格式不对。")]


# ---------- 回合入口 ----------

async def test_process_first_text_no_marker(monkeypatch):
    scheduled = []

    async def fake_execute(key, spec):
        scheduled.append(spec)

    monkeypatch.setattr(alarm_bridge, "execute", fake_execute)
    text, consumed = alarm_bridge.process_first_text("dev-1", "今天天气不错。")
    assert (text, consumed) == ("今天天气不错。", False)
    await asyncio.sleep(0)
    assert scheduled == []


async def test_process_first_text_marker_strips_and_schedules(monkeypatch):
    scheduled = []

    async def fake_execute(key, spec):
        scheduled.append((key, spec))

    monkeypatch.setattr(alarm_bridge, "execute", fake_execute)
    text, consumed = alarm_bridge.process_first_text(
        "dev-1", '【闹钟】{"action":"create","time":"07:30"}\n好的，记下了。')
    assert consumed is True and text == "好的，记下了。"
    await asyncio.sleep(0)
    assert scheduled == [("dev-1", {"action": "create", "time": "07:30"})]


async def test_process_first_text_bad_marker_announces_failure(monkeypatch, announced):
    text, consumed = alarm_bridge.process_first_text("dev-1", "【闹钟】{坏的}\n好的。")
    assert consumed is True and text == "好的。"
    assert len(announced) == 1 and announced[0][2].startswith("闹钟没设成：")


# ---------- 双来源查标记（最终文本会丢开头标记，增量里找补） ----------

async def test_process_first_text_marker_only_in_streamed(monkeypatch):
    scheduled = []

    async def fake_execute(key, spec):
        scheduled.append((key, spec))

    monkeypatch.setattr(alarm_bridge, "execute", fake_execute)
    final = "好的，五分钟后叫你。"
    streamed = '【闹钟】{"action":"create","time":"08:04","repeat":"once","label":"五分钟后"}\n好的，五分钟后叫你。'
    text, consumed = alarm_bridge.process_first_text("dev-1", final, streamed)
    assert consumed is True and text == final  # 最终文本不含标记行，原样下发
    await asyncio.sleep(0)
    assert scheduled == [("dev-1", {"action": "create", "time": "08:04", "repeat": "once", "label": "五分钟后"})]


async def test_process_first_text_marker_only_in_final(monkeypatch):
    scheduled = []

    async def fake_execute(key, spec):
        scheduled.append((key, spec))

    monkeypatch.setattr(alarm_bridge, "execute", fake_execute)
    final = '【闹钟】{"action":"list"}\n你现在没有闹钟。'
    text, consumed = alarm_bridge.process_first_text("dev-1", final, "你现在没有闹钟。")
    assert consumed is True and text == "你现在没有闹钟。"
    await asyncio.sleep(0)
    assert scheduled == [("dev-1", {"action": "list"})]


async def test_process_first_text_marker_in_both_sources_executes_once(monkeypatch):
    scheduled = []

    async def fake_execute(key, spec):
        scheduled.append((key, spec))

    monkeypatch.setattr(alarm_bridge, "execute", fake_execute)
    body = '【闹钟】{"action":"create","time":"07:30"}\n好的，记下了。'
    text, consumed = alarm_bridge.process_first_text("dev-1", body, body)
    assert consumed is True and text == "好的，记下了。"
    await asyncio.sleep(0)
    assert len(scheduled) == 1  # 两个来源都有标记也只执行一次


async def test_process_first_text_bad_marker_only_in_streamed(monkeypatch, announced):
    text, consumed = alarm_bridge.process_first_text("dev-1", "好的。", "【闹钟】{坏的}\n好的。")
    assert consumed is True and text == "好的。"
    assert len(announced) == 1 and announced[0][2].startswith("闹钟没设成：")


async def test_process_first_text_no_marker_in_either_source(monkeypatch):
    scheduled = []

    async def fake_execute(key, spec):
        scheduled.append(spec)

    monkeypatch.setattr(alarm_bridge, "execute", fake_execute)
    text, consumed = alarm_bridge.process_first_text("dev-1", "今天天气不错。", "今天天气不错。")
    assert (text, consumed) == ("今天天气不错。", False)
    await asyncio.sleep(0)
    assert scheduled == []


# ---------- 结果投递：只念不记 ----------

def test_announce_adds_report_without_record(monkeypatch):
    seen = []
    monkeypatch.setattr(
        reports, "add",
        lambda key, bot_id, text, record=True: seen.append((key, bot_id, text, record)))
    alarm_bridge.announce("dev-1", "闹钟已设好。")
    assert seen == [("dev-1", "alarm", "闹钟已设好。", False)]


class FakeTTS:
    def __init__(self):
        import queue

        self.calls = []
        self.tts_text_queue = queue.Queue()

    def store_tts_text(self, sentence_id, text):
        self.calls.append(("store", text))

    def tts_one_sentence(self, conn, content_type, content_detail=None):
        self.calls.append(("sentence", content_detail))


class SpeakConn:
    def __init__(self):
        from types import SimpleNamespace

        self.session_id, self.device_id = "s-1", "dev-1"
        self.client_is_speaking, self.client_have_voice = False, False
        self.tts = FakeTTS()
        self.loop = None  # add() 时不自动安排投递，测试里直接调 deliver_when_idle
        self.client_abort, self.sentence_id = True, None
        self.logger = SimpleNamespace(
            bind=lambda **k: SimpleNamespace(info=lambda *a: None, warning=lambda *a: None))


def test_reports_speak_record_false_never_reaches_speak_txt(monkeypatch):
    import core.handle.intentHandler as intent_mod

    recorded = []
    monkeypatch.setattr(intent_mod, "speak_txt", lambda conn, text: recorded.append(text))
    conn = SpeakConn()
    reports.speak(conn, "闹钟已设好。", record=False)
    assert recorded == []  # 没走会把文本写进对话的 speak_txt
    assert ("sentence", "闹钟已设好。") in conn.tts.calls  # 文本只进了 TTS 队列
    assert conn.sentence_id in conn.announce_sentence_ids  # 句已登记，下发时打 announce 标记
    reports.speak(conn, "普通播报。")
    assert recorded == ["普通播报。"]  # 默认路径不变：照常走 speak_txt 入对话


async def test_deliver_mixed_segments_only_recorded_goes_to_speak_txt(monkeypatch):
    from core.admin import connections
    import core.handle.intentHandler as intent_mod

    recorded = []
    monkeypatch.setattr(intent_mod, "speak_txt", lambda conn, text: recorded.append(text))
    conn = SpeakConn()
    reports.clear()
    connections.register(conn)
    try:
        reports.add("dev-1", "grok", "普通待汇报。")
        reports.add("dev-1", "alarm", "闹钟已设好。", record=False)
        assert await reports.deliver_when_idle(conn) is True
    finally:
        connections.unregister(conn)
        reports.clear()
    assert recorded == ["普通待汇报。"]  # 只念不记的那段没进对话路径
    assert ("sentence", "闹钟已设好。") in conn.tts.calls


# ---------- 首行前缀门（流式判定：分叉即放行、真标记整行吞） ----------

def test_gate_normal_first_char_releases_immediately():
    gate = alarm_bridge.FirstLineGate()
    release, marker = gate.feed("好")
    # 普通回复第一个字就与标记分叉：不等首行齐，立即原样放行
    assert marker is None and release == "好" and gate.decided
    release2, marker2 = gate.feed("的，我看一下。")
    assert marker2 is None and release + release2 == "好的，我看一下。"


def test_gate_normal_reply_pieces_concatenate_identically():
    gate = alarm_bridge.FirstLineGate()
    out = ""
    for piece in ["今", "天", "天气", "不错，", "适合出门。"]:
        release, marker = gate.feed(piece)
        assert marker is None
        out += release
    out += gate.finish()[0]
    assert out == "今天天气不错，适合出门。"


def test_gate_bracket_but_second_char_diverges_releases():
    gate = alarm_bridge.FirstLineGate()
    release, marker = gate.feed("【")
    assert (release, marker) == ("", None) and not gate.decided  # 还可能是标记，先攒
    release, marker = gate.feed("播报】今天有雨。")
    assert marker is None and gate.decided
    assert release == "【播报】今天有雨。"  # 【播报】类：第二字分叉，整段原样放行


def test_gate_divergence_after_partial_prefix():
    gate = alarm_bridge.FirstLineGate()
    assert gate.feed("【闹") == ("", None)
    release, marker = gate.feed("钟表坏了。")
    assert marker is None and release == "【闹钟表坏了。"


def test_gate_marker_split_across_deltas_swallowed_whole():
    gate = alarm_bridge.FirstLineGate()
    line = '【闹钟】{"action":"create","time":"08:38","repeat":"once","label":"三分钟后"}'
    pieces = ["【闹", "钟】{\"action\":\"cre", "ate\",\"time\":\"08:38\",\"repeat\":\"once\",\"label\":\"三分钟后\"}", "\n好的，定上了。"]
    releases, markers = [], []
    for piece in pieces:
        release, marker = gate.feed(piece)
        releases.append(release)
        if marker is not None:
            markers.append(marker)
    assert markers == [line]  # 跨多个 delta 拆分到达，仍整行交出、只交一次
    joined = "".join(releases)
    assert joined == "好的，定上了。"  # 放行文本里不含标记的任何字符
    assert "闹钟" not in joined and "action" not in joined


def test_gate_marker_only_line_held_until_finish():
    gate = alarm_bridge.FirstLineGate()
    line = '【闹钟】{"action":"list"}'
    release, marker = gate.feed(line)
    assert (release, marker) == ("", None)  # 没换行，先扣住，不放行任何文本
    release, marker = gate.finish()
    assert marker == line and release == ""


def test_gate_partial_prefix_released_on_finish():
    gate = alarm_bridge.FirstLineGate()
    assert gate.feed("【闹") == ("", None)
    release, marker = gate.finish()
    assert marker is None and release == "【闹"  # 残缺前缀不是标记，流结束原样放行


def test_gate_overlong_first_line_released_as_text():
    gate = alarm_bridge.FirstLineGate()
    text = "【闹钟】" + "长" * 400  # 以标记开头但整行迟迟不结束、超过缓冲上限
    release, marker = gate.feed(text)
    assert marker is None and release == text and gate.decided


async def test_gate_marker_line_dispatches_execute_once(monkeypatch):
    scheduled = []

    async def fake_execute(key, spec):
        scheduled.append((key, spec))

    monkeypatch.setattr(alarm_bridge, "execute", fake_execute)
    gate = alarm_bridge.FirstLineGate()
    _, marker = gate.feed('【闹钟】{"action":"create","time":"07:30"}\n好的。')
    assert marker is not None
    assert alarm_bridge.handle_marker_line("dev-1", marker, source="增量") is True
    await asyncio.sleep(0)
    assert scheduled == [("dev-1", {"action": "create", "time": "07:30"})]
    assert alarm_bridge.handle_marker_line("dev-1", "好的。") is False  # 非标记行不处理
    await asyncio.sleep(0)
    assert len(scheduled) == 1


# ---------- 增量命中时，最终文本里被改了模样的标记行也要剥 ----------

def test_strip_marker_from_final_variants():
    line = '【闹钟】{"action":"list"}'
    # 最终文本原样保留标记行 → 剥
    assert alarm_bridge.strip_marker_from_final(line + "\n你没有闹钟。", line) == "你没有闹钟。"
    # 最终文本丢掉前缀、只剩 JSON → 剥
    assert alarm_bridge.strip_marker_from_final('{"action":"list"}\n你没有闹钟。', line) == "你没有闹钟。"
    # 最终文本已把整行丢掉、正文就是首行 → 不动
    assert alarm_bridge.strip_marker_from_final("你没有闹钟。", line) == "你没有闹钟。"


async def test_process_first_text_streamed_hit_strips_bare_json_line(monkeypatch):
    scheduled = []

    async def fake_execute(key, spec):
        scheduled.append((key, spec))

    monkeypatch.setattr(alarm_bridge, "execute", fake_execute)
    # 2026-11-19 08:35 实测形态：最终文本把标记前缀丢了、只剩 JSON 一行，增量里完整
    final = '{"action":"create","time":"08:38","repeat":"once","label":"三分钟后"}\n好的，三分钟后的闹钟定上了。'
    streamed = '【闹钟】{"action":"create","time":"08:38","repeat":"once","label":"三分钟后"}\n好的，三分钟后的闹钟定上了。'
    text, consumed = alarm_bridge.process_first_text("dev-1", final, streamed)
    assert consumed is True
    assert text == "好的，三分钟后的闹钟定上了。"  # JSON 行被剥掉，不进 TTS 和对话
    await asyncio.sleep(0)
    assert len(scheduled) == 1


# ---------- 倒计时域：标记提取与参数 ----------

def test_timer_extract_create():
    text = '【倒计时】{"action":"create","duration_sec":600,"label":"煮面"}\n好的，开始计时。'
    stripped, spec, error = alarm_bridge.extract_timer(text)
    assert error is None
    assert spec == {"action": "create", "duration_sec": 600, "label": "煮面"}
    assert stripped == "好的，开始计时。"


def test_timer_extract_duration_string_coerced():
    _, spec, error = alarm_bridge.extract_timer('【倒计时】{"action":"create","duration_sec":"900"}')
    assert error is None and spec["duration_sec"] == 900


def test_timer_extract_missing_and_bad_duration():
    _, spec, error = alarm_bridge.extract_timer('【倒计时】{"action":"create"}')
    assert spec is None and error == "缺少时长"
    _, spec, error = alarm_bridge.extract_timer('【倒计时】{"action":"create","duration_sec":0}')
    assert spec is None and error and "24小时" in error
    _, spec, error = alarm_bridge.extract_timer('【倒计时】{"action":"create","duration_sec":"十分钟"}')
    assert spec is None and error and "总秒数" in error


def test_timer_extract_cancel_without_id_ok():
    # 与闹钟域不同：cancel/pause/resume 的 id 可省略，桥执行前查列表锁定
    _, spec, error = alarm_bridge.extract_timer('【倒计时】{"action":"cancel"}')
    assert error is None and spec == {"action": "cancel"}


def test_timer_extract_unknown_action():
    _, spec, error = alarm_bridge.extract_timer('【倒计时】{"action":"explode"}')
    assert spec is None and error and "explode" in error


def test_timer_action_tool_mapping_and_args():
    assert alarm_bridge.TIMER_ACTION_TOOLS == {
        "create": "self.timer.create",
        "list": "self.timer.list",
        "cancel": "self.timer.cancel",
        "pause": "self.timer.pause",
        "resume": "self.timer.resume",
    }
    assert alarm_bridge.build_timer_args({"action": "create", "duration_sec": 600, "label": "煮面"}) == {
        "duration_sec": 600, "label": "煮面"}
    assert alarm_bridge.build_timer_args({"action": "cancel", "id": "t1"}) == {"id": "t1"}
    assert alarm_bridge.build_timer_args({"action": "list"}) == {}


# ---------- 倒计时域：文案 ----------

def test_duration_text():
    assert alarm_bridge.duration_text(45) == "45秒"
    assert alarm_bridge.duration_text(60) == "1分钟"
    assert alarm_bridge.duration_text(600) == "10分钟"
    assert alarm_bridge.duration_text(570) == "9分30秒"
    assert alarm_bridge.duration_text(3600) == "1小时"
    assert alarm_bridge.duration_text(3660) == "1小时1分"


def test_timer_success_text_create():
    spec = {"action": "create", "duration_sec": 600, "label": "煮面"}
    data = {"success": True, "id": "t1", "duration_sec": 600, "label": "煮面",
            "ends_at_local": "2026-11-19T15:42:00"}
    assert alarm_bridge.timer_success_text(spec, data, now=NOW) == "倒计时已设好，煮面，10分钟，3点42分到点。"


def test_timer_success_text_list():
    data = {"success": True, "timers": [
        {"id": "t1", "label": "煮面", "state": "running", "remaining_sec": 570},
        {"id": "t2", "label": "泡茶", "state": "paused", "remaining_sec": 180},
    ]}
    assert alarm_bridge.timer_success_text({"action": "list"}, data) == (
        "你一共有2个倒计时：煮面，还剩9分30秒；泡茶，已暂停，还剩3分钟。")
    assert alarm_bridge.timer_success_text({"action": "list"}, {"success": True, "timers": []}) == "你现在没有倒计时。"


def test_timer_success_text_cancel_pause_resume():
    assert alarm_bridge.timer_success_text(
        {"action": "cancel", "_label": "煮面"}, {"success": True}) == "已经取消了煮面的倒计时。"
    assert alarm_bridge.timer_success_text(
        {"action": "cancel"}, {"success": True}) == "倒计时已经取消了。"
    assert alarm_bridge.timer_success_text(
        {"action": "pause"}, {"success": True, "remaining_sec": 570}) == "倒计时已暂停，还剩9分30秒。"
    ends_at = NOW.timestamp() * 1000 + 570_000
    assert alarm_bridge.timer_success_text(
        {"action": "resume"}, {"success": True, "ends_at": ends_at}, now=NOW) == "倒计时继续，9分30秒后到点。"


def test_timer_failure_text_prefix_per_action():
    assert alarm_bridge.timer_failure_text({"action": "create"}, "时长不对") == "倒计时没设成：时长不对。"
    assert alarm_bridge.timer_failure_text({"action": "pause"}, "没有正在跑的") == "倒计时没暂停成：没有正在跑的。"
    assert alarm_bridge.timer_failure_text(None, "格式不对") == "倒计时没设成：格式不对。"


# ---------- 倒计时域：执行（mock 工具调用与待汇报箱） ----------

async def test_timer_execute_create(monkeypatch, announced):
    conn = FakeConn(asyncio.get_running_loop())
    monkeypatch.setattr(alarm_bridge, "_find_conn", lambda key: conn)
    calls = []

    async def fake_call(conn_, client, tool_name, args, timeout=30):
        calls.append((tool_name, json.loads(args)))
        return json.dumps({"success": True, "id": "t1", "duration_sec": 600, "label": "煮面",
                           "ends_at_local": "2026-11-19T15:42:00"})

    monkeypatch.setattr(mcp_handler, "call_mcp_tool", fake_call)
    await alarm_bridge.execute_timer("dev-1", {"action": "create", "duration_sec": 600, "label": "煮面"})
    assert calls == [("self_timer_create", {"duration_sec": 600, "label": "煮面"})]
    assert announced == [("dev-1", "timer", "倒计时已设好，煮面，10分钟，3点42分到点。")]


async def test_timer_execute_cancel_resolves_single_active(monkeypatch, announced):
    conn = FakeConn(asyncio.get_running_loop())
    monkeypatch.setattr(alarm_bridge, "_find_conn", lambda key: conn)
    calls = []

    async def fake_call(conn_, client, tool_name, args, timeout=30):
        calls.append(tool_name)
        if tool_name == "self_timer_list":
            return json.dumps({"success": True, "timers": [
                {"id": "t1", "label": "煮面", "state": "running", "remaining_sec": 300}]})
        return json.dumps({"success": True})

    monkeypatch.setattr(mcp_handler, "call_mcp_tool", fake_call)
    await alarm_bridge.execute_timer("dev-1", {"action": "cancel"})
    assert calls == ["self_timer_list", "self_timer_cancel"]  # 无 id 先查列表锁定唯一一条
    assert announced == [("dev-1", "timer", "已经取消了煮面的倒计时。")]


async def test_timer_execute_cancel_ambiguous_announces_failure(monkeypatch, announced):
    conn = FakeConn(asyncio.get_running_loop())
    monkeypatch.setattr(alarm_bridge, "_find_conn", lambda key: conn)
    calls = []

    async def fake_call(conn_, client, tool_name, args, timeout=30):
        calls.append(tool_name)
        return json.dumps({"success": True, "timers": [
            {"id": "t1", "label": "煮面", "state": "running", "remaining_sec": 300},
            {"id": "t2", "label": "泡茶", "state": "running", "remaining_sec": 120}]})

    monkeypatch.setattr(mcp_handler, "call_mcp_tool", fake_call)
    await alarm_bridge.execute_timer("dev-1", {"action": "cancel"})
    assert calls == ["self_timer_list"]  # 定不下来就不许乱取消
    assert len(announced) == 1
    key, bot_id, text = announced[0]
    assert (key, bot_id) == ("dev-1", "timer")
    assert text.startswith("倒计时没取消成：") and "2个" in text


async def test_timer_execute_pause_resolves_by_label(monkeypatch, announced):
    conn = FakeConn(asyncio.get_running_loop())
    monkeypatch.setattr(alarm_bridge, "_find_conn", lambda key: conn)
    calls = []

    async def fake_call(conn_, client, tool_name, args, timeout=30):
        calls.append((tool_name, json.loads(args)))
        if tool_name == "self_timer_list":
            return json.dumps({"success": True, "timers": [
                {"id": "t1", "label": "煮面", "state": "running", "remaining_sec": 300},
                {"id": "t2", "label": "泡茶", "state": "running", "remaining_sec": 120}]})
        return json.dumps({"success": True, "remaining_sec": 118})

    monkeypatch.setattr(mcp_handler, "call_mcp_tool", fake_call)
    await alarm_bridge.execute_timer("dev-1", {"action": "pause", "label": "泡茶"})
    assert calls[-1] == ("self_timer_pause", {"id": "t2"})
    assert announced == [("dev-1", "timer", "倒计时已暂停，还剩1分58秒。")]


async def test_timer_execute_device_offline(monkeypatch, announced):
    monkeypatch.setattr(alarm_bridge, "_find_conn", lambda key: None)
    await alarm_bridge.execute_timer("dev-1", {"action": "create", "duration_sec": 60})
    assert announced == [("dev-1", "timer", "倒计时没设成：设备不在线。")]


async def test_timer_execute_tool_missing(monkeypatch, announced):
    conn = FakeConn(asyncio.get_running_loop())
    monkeypatch.setattr(alarm_bridge, "_find_conn", lambda key: conn)

    async def fake_call(conn_, client, tool_name, args, timeout=30):
        raise ValueError(f"工具 {tool_name} 不存在")

    monkeypatch.setattr(mcp_handler, "call_mcp_tool", fake_call)
    await alarm_bridge.execute_timer("dev-1", {"action": "list"})
    assert announced == [("dev-1", "timer", "倒计时列表没查成：设备上还没有倒计时功能。")]


# ---------- 倒计时域：回合入口与双来源 ----------

async def test_process_first_text_timer_marker_in_final(monkeypatch):
    scheduled = []

    async def fake_execute_timer(key, spec):
        scheduled.append((key, spec))

    monkeypatch.setattr(alarm_bridge, "execute_timer", fake_execute_timer)
    text, consumed = alarm_bridge.process_first_text(
        "dev-1", '【倒计时】{"action":"create","duration_sec":600,"label":"煮面"}\n好的，开始计时。')
    assert consumed is True and text == "好的，开始计时。"
    await asyncio.sleep(0)
    assert scheduled == [("dev-1", {"action": "create", "duration_sec": 600, "label": "煮面"})]


async def test_process_first_text_timer_marker_only_in_streamed(monkeypatch):
    scheduled = []

    async def fake_execute_timer(key, spec):
        scheduled.append((key, spec))

    monkeypatch.setattr(alarm_bridge, "execute_timer", fake_execute_timer)
    final = "好的，十分钟后叫你。"
    streamed = '【倒计时】{"action":"create","duration_sec":600}\n好的，十分钟后叫你。'
    text, consumed = alarm_bridge.process_first_text("dev-1", final, streamed)
    assert consumed is True and text == final  # 最终文本不含标记行，原样下发
    await asyncio.sleep(0)
    assert scheduled == [("dev-1", {"action": "create", "duration_sec": 600})]


async def test_process_first_text_timer_streamed_hit_strips_bare_json(monkeypatch):
    scheduled = []

    async def fake_execute_timer(key, spec):
        scheduled.append((key, spec))

    monkeypatch.setattr(alarm_bridge, "execute_timer", fake_execute_timer)
    # 与闹钟 08:35 同形态：最终文本丢了【倒计时】前缀、只剩 JSON 一行
    final = '{"action":"create","duration_sec":600}\n好的，开始计时。'
    streamed = '【倒计时】{"action":"create","duration_sec":600}\n好的，开始计时。'
    text, consumed = alarm_bridge.process_first_text("dev-1", final, streamed)
    assert consumed is True and text == "好的，开始计时。"
    await asyncio.sleep(0)
    assert len(scheduled) == 1


async def test_process_first_text_timer_bad_marker_announces_failure(monkeypatch, announced):
    text, consumed = alarm_bridge.process_first_text("dev-1", "【倒计时】{坏的}\n好的。")
    assert consumed is True and text == "好的。"
    assert len(announced) == 1 and announced[0][1] == "timer"
    assert announced[0][2].startswith("倒计时没设成：")


async def test_handle_marker_line_timer_dispatches(monkeypatch):
    scheduled = []

    async def fake_execute_timer(key, spec):
        scheduled.append((key, spec))

    monkeypatch.setattr(alarm_bridge, "execute_timer", fake_execute_timer)
    assert alarm_bridge.handle_marker_line("dev-1", '【倒计时】{"action":"pause"}', source="增量") is True
    await asyncio.sleep(0)
    assert scheduled == [("dev-1", {"action": "pause"})]


def test_strip_marker_from_final_timer_variants():
    line = '【倒计时】{"action":"list"}'
    assert alarm_bridge.strip_marker_from_final(line + "\n没有倒计时。", line) == "没有倒计时。"
    assert alarm_bridge.strip_marker_from_final('{"action":"list"}\n没有倒计时。', line) == "没有倒计时。"
    assert alarm_bridge.strip_marker_from_final("没有倒计时。", line) == "没有倒计时。"


# ---------- 首行前缀门：双标记集合（两标记只共享首字「【」） ----------

def test_gate_timer_prefix_waits_then_marker():
    gate = alarm_bridge.FirstLineGate()
    assert gate.feed("【倒") == ("", None) and not gate.decided  # 【倒计时】的真前缀，先攒
    release, marker = gate.feed('计时】{"action":"list"}\n好的。')
    assert marker == '【倒计时】{"action":"list"}'
    assert release == "好的。"


def test_gate_timer_marker_split_across_deltas():
    gate = alarm_bridge.FirstLineGate()
    line = '【倒计时】{"action":"create","duration_sec":600}'
    releases, markers = [], []
    for piece in ["【倒", "计时】{\"action\":\"cre", "ate\",\"duration_sec\":600}", "\n好的。"]:
        release, marker = gate.feed(piece)
        releases.append(release)
        if marker is not None:
            markers.append(marker)
    assert markers == [line]  # 跨 delta 拆分仍整行交出、只交一次
    assert "".join(releases) == "好的。"  # 放行文本不含标记任何字符


def test_gate_timer_divergence_after_partial_prefix_releases():
    gate = alarm_bridge.FirstLineGate()
    assert gate.feed("【倒") == ("", None)
    release, marker = gate.feed("计时器坏了。")
    # 「【倒计时器」第五字与【倒计时】分叉（器 vs 】），与【闹钟】第二字就分叉：普通文本
    assert marker is None and release == "【倒计时器坏了。" and gate.decided


def test_gate_alarm_divergence_still_releases_with_marker_set():
    gate = alarm_bridge.FirstLineGate()
    assert gate.feed("【闹") == ("", None)
    release, marker = gate.feed("钟声响了。")
    # 「【闹钟声」第四字与【闹钟】分叉（声 vs 】）：普通文本原样放行，不被倒计时候选拖住
    assert marker is None and release == "【闹钟声响了。" and gate.decided


def test_gate_timer_marker_only_line_held_until_finish():
    gate = alarm_bridge.FirstLineGate()
    line = '【倒计时】{"action":"list"}'
    assert gate.feed(line) == ("", None)  # 没换行先扣住
    release, marker = gate.finish()
    assert marker == line and release == ""


# ---------- 静默收尾：倒计时 create 成功不回复 ----------

def _fake_timer_conn(monkeypatch, result_json):
    conn = FakeConn(asyncio.get_running_loop())
    monkeypatch.setattr(alarm_bridge, "_find_conn", lambda key: conn)
    calls = []

    async def fake_call(conn_, client, tool_name, args, timeout=30):
        calls.append((tool_name, json.loads(args)))
        return result_json

    monkeypatch.setattr(mcp_handler, "call_mcp_tool", fake_call)
    return calls


async def test_timer_create_silent_success_no_announce(monkeypatch, announced):
    calls = _fake_timer_conn(monkeypatch, json.dumps(
        {"success": True, "id": "t1", "duration_sec": 60, "ends_at_local": "2026-11-19T08:01:00"}))
    ok = await alarm_bridge.execute_timer_create_silent(
        "dev-1", {"action": "create", "duration_sec": 60})
    assert ok is True
    assert calls == [("self_timer_create", {"duration_sec": 60})]
    assert announced == []  # 成功不播报任何文字


async def test_timer_create_silent_failure_still_announces(monkeypatch, announced):
    _fake_timer_conn(monkeypatch, json.dumps({"success": False, "error": "时长不对"}))
    ok = await alarm_bridge.execute_timer_create_silent(
        "dev-1", {"action": "create", "duration_sec": 60})
    assert ok is False
    assert announced == [("dev-1", "timer", "倒计时没设成：时长不对。")]  # 不能静默失败


async def test_timer_create_silent_offline_announces_failure(monkeypatch, announced):
    monkeypatch.setattr(alarm_bridge, "_find_conn", lambda key: None)
    ok = await alarm_bridge.execute_timer_create_silent(
        "dev-1", {"action": "create", "duration_sec": 60})
    assert ok is False
    assert announced == [("dev-1", "timer", "倒计时没设成：设备不在线。")]


async def test_settle_first_text_timer_create_success_discards_rest(monkeypatch, announced):
    _fake_timer_conn(monkeypatch, json.dumps(
        {"success": True, "id": "t1", "duration_sec": 60, "ends_at_local": "2026-11-19T08:01:00"}))
    text, consumed, silenced = await alarm_bridge.settle_first_text(
        "dev-1", '【倒计时】{"action":"create","duration_sec":60}\n好，一分钟后提醒你。')
    assert (text, consumed, silenced) == ("", True, True)  # 余文整段作废
    assert announced == []


async def test_settle_first_text_timer_create_failure_keeps_rest(monkeypatch, announced):
    _fake_timer_conn(monkeypatch, json.dumps({"success": False, "error": "时长不对"}))
    text, consumed, silenced = await alarm_bridge.settle_first_text(
        "dev-1", '【倒计时】{"action":"create","duration_sec":60}\n好，一分钟后提醒你。')
    assert (text, consumed, silenced) == ("好，一分钟后提醒你。", True, False)
    assert announced == [("dev-1", "timer", "倒计时没设成：时长不对。")]


async def test_settle_first_text_timer_create_only_in_streamed_silences(monkeypatch, announced):
    _fake_timer_conn(monkeypatch, json.dumps(
        {"success": True, "id": "t1", "duration_sec": 60, "ends_at_local": "2026-11-19T08:01:00"}))
    final = "好，一分钟后提醒你。"
    streamed = '【倒计时】{"action":"create","duration_sec":60}\n好，一分钟后提醒你。'
    text, consumed, silenced = await alarm_bridge.settle_first_text("dev-1", final, streamed)
    assert (text, consumed, silenced) == ("", True, True)
    assert announced == []


async def test_settle_first_text_alarm_create_not_silenced(monkeypatch, announced):
    # 反例：闹钟 create 不走静默——照旧安排执行、余文照常下发
    scheduled = []

    async def fake_execute(key, spec):
        scheduled.append((key, spec))

    monkeypatch.setattr(alarm_bridge, "execute", fake_execute)
    text, consumed, silenced = await alarm_bridge.settle_first_text(
        "dev-1", '【闹钟】{"action":"create","time":"07:30"}\n好的，明早叫你。')
    assert (text, consumed, silenced) == ("好的，明早叫你。", True, False)
    await asyncio.sleep(0)
    assert scheduled == [("dev-1", {"action": "create", "time": "07:30"})]


async def test_settle_first_text_timer_list_not_silenced(monkeypatch):
    # 倒计时的其他动作也不静默：照旧安排 execute_timer、余文照常
    scheduled = []

    async def fake_execute_timer(key, spec):
        scheduled.append((key, spec))

    monkeypatch.setattr(alarm_bridge, "execute_timer", fake_execute_timer)
    text, consumed, silenced = await alarm_bridge.settle_first_text(
        "dev-1", '【倒计时】{"action":"list"}\n你现在没有倒计时。')
    assert (text, consumed, silenced) == ("你现在没有倒计时。", True, False)
    await asyncio.sleep(0)
    assert scheduled == [("dev-1", {"action": "list"})]


async def test_settle_first_text_timer_bad_marker_not_silenced(monkeypatch, announced):
    # 非法倒计时标记走原失败路径：余文照常＋失败补播报，不进静默判定
    text, consumed, silenced = await alarm_bridge.settle_first_text(
        "dev-1", "【倒计时】{坏的}\n好的。")
    assert (text, consumed, silenced) == ("好的。", True, False)
    assert len(announced) == 1 and announced[0][2].startswith("倒计时没设成：")


async def test_settle_marker_line_timer_create_success(monkeypatch, announced):
    _fake_timer_conn(monkeypatch, json.dumps(
        {"success": True, "id": "t1", "duration_sec": 60, "ends_at_local": "2026-11-19T08:01:00"}))
    line = '【倒计时】{"action":"create","duration_sec":60}'
    text, silenced = await alarm_bridge.settle_marker_line(
        "dev-1", line, line + "\n好，一分钟后提醒你。", source="增量")
    assert (text, silenced) == ("", True)
    assert announced == []


async def test_settle_marker_line_timer_create_failure(monkeypatch, announced):
    _fake_timer_conn(monkeypatch, json.dumps({"success": False, "error": "时长不对"}))
    line = '【倒计时】{"action":"create","duration_sec":60}'
    text, silenced = await alarm_bridge.settle_marker_line(
        "dev-1", line, line + "\n好，一分钟后提醒你。", source="增量")
    assert (text, silenced) == ("好，一分钟后提醒你。", False)  # 失败：剥行后余文照常
    assert announced == [("dev-1", "timer", "倒计时没设成：时长不对。")]


async def test_settle_marker_line_alarm_create_not_silenced(monkeypatch):
    scheduled = []

    async def fake_execute(key, spec):
        scheduled.append((key, spec))

    monkeypatch.setattr(alarm_bridge, "execute", fake_execute)
    line = '【闹钟】{"action":"create","time":"07:30"}'
    text, silenced = await alarm_bridge.settle_marker_line(
        "dev-1", line, line + "\n好的，明早叫你。", source="增量")
    assert (text, silenced) == ("好的，明早叫你。", False)
    await asyncio.sleep(0)
    assert scheduled == [("dev-1", {"action": "create", "time": "07:30"})]

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
    monkeypatch.setattr(reports, "add", lambda key, bot_id, text: out.append((key, bot_id, text)))
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

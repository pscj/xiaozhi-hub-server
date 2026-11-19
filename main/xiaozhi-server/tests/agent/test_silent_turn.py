"""经典通道静默收尾判定：整轮只有倒计时 create 且全成功才静默（纯逻辑）。

闹钟不纳入静默（用户明确闹钟保持现状）：纯 self_alarm_create、或与倒计时
create 混合，都不静默——这两条是反例。
"""

import json

from plugins_func.register import Action, ActionResponse

from core.utils.silent_turn import tool_result_succeeded, turn_silent_after_tools


def _reqllm(payload):
    return ActionResponse(action=Action.REQLLM, result=json.dumps(payload))


# ---------- turn_silent_after_tools ----------

def test_all_timer_create_success_is_silent():
    assert turn_silent_after_tools([("self_timer_create", True)]) is True
    assert turn_silent_after_tools(
        [("self_timer_create", True), ("self_timer_create", True)]) is True


def test_no_tools_not_silent():
    assert turn_silent_after_tools([]) is False


def test_mixed_with_list_not_silent():
    assert turn_silent_after_tools(
        [("self_timer_list", True), ("self_timer_create", True)]) is False
    assert turn_silent_after_tools(
        [("self_timer_create", True), ("self_timer_list", True)]) is False


def test_mixed_with_alarm_create_not_silent():
    assert turn_silent_after_tools([("self_alarm_create", True)]) is False
    assert turn_silent_after_tools(
        [("self_timer_create", True), ("self_alarm_create", True)]) is False


def test_any_failure_not_silent():
    assert turn_silent_after_tools([("self_timer_create", False)]) is False
    assert turn_silent_after_tools(
        [("self_timer_create", True), ("self_timer_create", False)]) is False


# ---------- tool_result_succeeded ----------

def test_reqllm_device_success():
    assert tool_result_succeeded(_reqllm({"success": True, "id": "t1"})) is True


def test_reqllm_device_failure_payload():
    assert tool_result_succeeded(_reqllm({"success": False, "error": "时长不对"})) is False
    assert tool_result_succeeded(_reqllm({"error": "设备出错"})) is False


def test_reqllm_without_failure_marks_is_success():
    assert tool_result_succeeded(_reqllm({"timers": []})) is True
    assert tool_result_succeeded(ActionResponse(action=Action.REQLLM, result="已完成")) is True


def test_error_actions_are_failures():
    assert tool_result_succeeded(ActionResponse(action=Action.ERROR, response="超时")) is False
    assert tool_result_succeeded(ActionResponse(action=Action.NOTFOUND, response="没有这个工具")) is False
    assert tool_result_succeeded(None) is False

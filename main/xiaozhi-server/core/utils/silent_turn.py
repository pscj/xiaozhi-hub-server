"""经典通道的静默收尾判定（纯逻辑，可单测）。

用户要求：语音新建倒计时成功后不要回复任何文字——倒计时屏本身就是反馈。
经典 function_call 链里，设备工具（self.timer.create）执行完默认还要再问
一轮模型生成确认文字（如「好，新的六十秒计时已经开始了」）并合成播报；
整轮只成功创建了倒计时时跳过这一轮跟进生成，回合无文字收尾（判定与跳过
点在 core/connection.py 的 _handle_function_result）。混入任何其他工具
（含查列表、闹钟创建）或任一工具失败都不静默，维持原流程。
"""

import json

from plugins_func.register import Action

# 设备倒计时创建工具在 LLM 侧的消毒名（self.timer.create → self_timer_create）
TIMER_CREATE_TOOL = "self_timer_create"


def tool_result_succeeded(result):
    """一个工具结果是否算成功：执行层没报错，设备返回里也没报失败。

    设备 MCP 工具成功时是 Action.REQLLM、result 为设备返回的 JSON 文本；
    设备在 JSON 里报 success=false / error 时算失败（与闹钟桥 _result_error
    同口径）。传输与执行异常是 Action.ERROR / NOTFOUND，也不算成功。
    """
    if result is None:
        return False
    if result.action in (Action.ERROR, Action.NOTFOUND):
        return False
    raw = result.result if result.result is not None else result.response
    data = raw if isinstance(raw, (dict, list)) else None
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
    if isinstance(data, dict):
        if data.get("success") is False:
            return False
        if data.get("error") and data.get("success") is not True:
            return False
    return True


def turn_silent_after_tools(tool_outcomes):
    """整轮是否静默收尾：执行过的工具全部是倒计时 create 且全部成功。

    tool_outcomes 是 [(工具名, 是否成功), ...]（一轮内跨 depth 累积的全部
    工具）。没执行过工具、混入任何其他工具、任一失败 → False。
    """
    if not tool_outcomes:
        return False
    return all(name == TIMER_CREATE_TOOL and ok for name, ok in tool_outcomes)

"""通过真实 OpenAI SDK 和内存 SSE 响应验证联网请求，不访问外网。"""
import copy
import json

import httpx
from httpx._client import Client
import openai
import pytest

from core.providers.llm.openai.openai import LLMProvider


SEARCH = {"type": "deepseek_web_search"}
FUNCTION = {"type": "function", "function": {
    "name": "get_volume", "parameters": {"type": "object", "properties": {}}
}}
DIALOGUE = [{"role": "user", "content": "请联网查询今天的新闻"}]


def chunk(delta=None, **choice_fields):
    return {"id": "test", "object": "chat.completion.chunk", "created": 0,
            "model": "deepseek-flash", "choices": [
                {"index": 0, "delta": delta or {}, "finish_reason": None, **choice_fields}
            ]}


@pytest.fixture
def api(monkeypatch):
    requests, chunks, clients = [], [], []
    real_openai = openai.OpenAI

    def handle(request):
        requests.append(json.loads(request.content))
        data = "".join(f"data: {json.dumps(item)}\n\n" for item in chunks)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=data + "data: [DONE]\n\n")

    def make_client(**kwargs):
        client = real_openai(**kwargs, http_client=Client(transport=httpx.MockTransport(handle)))
        clients.append(client)
        return client

    monkeypatch.setattr(openai, "OpenAI", make_client)
    yield requests, chunks
    for client in clients:
        client.close()


def provider(**overrides):
    return LLMProvider({"model_name": "deepseek-flash", "api_key": "sk-test",
                        "base_url": "https://api.deepseek.com", **overrides})


def respond(llm, mode, functions=None):
    if mode == "functions":
        return llm.response_with_functions("s", copy.deepcopy(DIALOGUE), functions=functions)
    return llm.response("s", copy.deepcopy(DIALOGUE))


@pytest.mark.parametrize("mode", ["plain", "functions"])
def test_search_on_both_paths_only_outputs_answer(api, mode):
    requests, chunks = api
    chunks.extend([
        chunk(server_tool_event={"type": "call_started", "step": 0, "call": {
            "id": "search-1", "type": "deepseek_web_search",
            "action": {"type": "search", "queries": ["今天 新闻"]}}}),
        chunk({"content": "今日"}),
        chunk({"content": "新闻"}),
        chunk({"server_tool_calls": [{"index": 0, "calls": []}],
               "annotations": [{"type": "url_citation", "url_citation": {
                   "url": "https://example.com", "title": "新闻", "start_index": 0, "end_index": 4}}]},
              finish_reason="stop"),
    ])
    functions = copy.deepcopy([FUNCTION])
    llm = provider()
    result = list(respond(llm, mode, functions))
    text = "".join((item[0] or "") if mode == "functions" else item for item in result)
    assert text == "今日新闻"
    assert requests[0]["tools"] == ([FUNCTION, SEARCH] if mode == "functions" else [SEARCH])
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert "tool_choice" not in requests[0]
    assert functions == [FUNCTION]
    assert llm._streams == {}
    if mode == "functions":
        assert all(tools is None for _, tools in result)


@pytest.mark.parametrize("mode", ["plain", "functions"])
@pytest.mark.parametrize("config,enabled", [
    ({"web_search": False}, False),
    ({"model_name": "deepseek-chat", "web_search": True}, False),
    ({"model_name": "deepseek-v4-flash", "web_search": True}, False),
    ({"base_url": "https://api.openai.com/v1", "model_name": "gpt-5"}, False),
    ({"base_url": "https://api.deepseek.com.example.org"}, False),
    ({"base_url": "https://proxy.example.org/v1"}, False),
    ({"base_url": "https://proxy.example.org/v1", "web_search": True}, True),
    ({"base_url": "https://api.deepseek.com:443/v1"}, True),
])
def test_search_switch_model_and_endpoint_compatibility(api, mode, config, enabled):
    requests, _ = api
    list(respond(provider(**config), mode, [FUNCTION]))
    tools = requests[0].get("tools") or []
    assert (SEARCH in tools) is enabled
    if mode == "functions":
        assert FUNCTION in tools


@pytest.mark.parametrize("mode", ["plain", "functions"])
@pytest.mark.parametrize("existing", [[FUNCTION], [FUNCTION, SEARCH]])
def test_extra_body_tools_not_duplicated_or_mutated(api, mode, existing):
    requests, _ = api
    extra = {"tools": copy.deepcopy(existing), "thinking": {"type": "enabled"}}
    original = copy.deepcopy(extra)
    llm = provider(extra_body=extra)
    # 多次调用不累加工具，也保留配置中显式开启的深度思考。
    for _ in range(2):
        list(respond(llm, mode, [FUNCTION]))
    assert all(req["tools"] == [FUNCTION, SEARCH] for req in requests)
    assert all(req["thinking"] == {"type": "enabled"} for req in requests)
    assert extra == original


def test_device_function_calls_still_reach_caller(api):
    requests, chunks = api
    chunks.append(chunk({"tool_calls": [{"index": 0, "id": "fn-1", "type": "function",
                                         "function": {"name": "get_volume", "arguments": "{}"}}]}))
    result = list(respond(provider(), "functions", [FUNCTION]))
    call = result[0][1][0]
    assert call.function.name == "get_volume" and call.function.arguments == "{}"
    assert requests[0]["tools"] == [FUNCTION, SEARCH]


@pytest.mark.parametrize("mode", ["plain", "functions"])
def test_generation_error_after_search_is_not_treated_as_success(api, mode):
    _, chunks = api
    chunks.extend([chunk({"content": "开始"}),
                   {"error": {"message": "Search generation failed", "type": "api_error"},
                    "choices": [], "usage": {"total_tokens": 10}}])
    llm = provider()
    stream = respond(llm, mode, [FUNCTION])
    next(stream)
    with pytest.raises(openai.APIError):
        list(stream)
    assert llm._streams == {}

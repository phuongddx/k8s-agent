import json

import httpx2
import pytest

from k8s_agent.llm import ModelReply, ModelUnavailable, OpenAIChatModel, ToolCall, parse_json_object


def _completion(message, usage=(120, 30)):
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 1,
        "model": "glm-5.3",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)},
    }


def _model(handler):
    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    return OpenAIChatModel("https://api.z.ai/api/coding/paas/v4", "zai-key", "glm-5.3", http_client=http)


def test_request_shape_and_tool_call_parsing():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json=_completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "events", "arguments": '{"namespace": "x"}'},
                        }
                    ],
                }
            ),
        )

    tools = [{"type": "function", "function": {"name": "events", "parameters": {"type": "object"}}}]
    reply = _model(handler).complete([{"role": "user", "content": "hi"}], tools)
    assert seen["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions"
    assert seen["auth"] == "Bearer zai-key"
    assert seen["body"]["model"] == "glm-5.3"
    assert seen["body"]["tools"] == tools
    assert reply.tool_calls == [ToolCall("call_1", "events", {"namespace": "x"}, '{"namespace": "x"}')]
    assert (reply.prompt_tokens, reply.completion_tokens) == (120, 30)
    assert reply.as_message()["tool_calls"][0]["function"]["name"] == "events"


def test_no_tools_key_when_no_tools():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=_completion({"role": "assistant", "content": "ok"}))

    assert _model(handler).complete([{"role": "user", "content": "hi"}]).content == "ok"
    assert "tools" not in seen["body"]


def test_one_retry_then_success():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx2.Response(503, json={"error": {"message": "busy"}}, headers={"retry-after-ms": "10"})
        return httpx2.Response(200, json=_completion({"role": "assistant", "content": "ok"}))

    assert _model(handler).complete([{"role": "user", "content": "hi"}]).content == "ok"
    assert len(attempts) == 2


def test_failure_after_one_retry_raises_model_unavailable():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx2.Response(429, json={"error": {"message": "rate"}}, headers={"retry-after-ms": "10"})

    with pytest.raises(ModelUnavailable):
        _model(handler).complete([{"role": "user", "content": "hi"}])
    assert len(attempts) == 2


def test_bad_tool_arguments_become_empty_dict():
    def handler(request):
        return httpx2.Response(
            200,
            json=_completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c", "type": "function", "function": {"name": "events", "arguments": "not json"}}
                    ],
                }
            ),
        )

    [call] = _model(handler).complete([{"role": "user", "content": "x"}]).tool_calls
    assert call.arguments == {}
    assert call.raw_arguments == "not json"


def test_parse_json_object():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Here: {"a": {"b": 2}} done') == {"a": {"b": 2}}
    for bad in ["", "no json", "[1, 2]", "{bad"]:
        with pytest.raises(ValueError):
            parse_json_object(bad)


def test_as_message_without_tool_calls():
    assert ModelReply("text").as_message() == {"role": "assistant", "content": "text"}


def test_empty_choices_is_model_unavailable():
    def handler(request):
        return httpx2.Response(200, json={**_completion({"role": "assistant", "content": "x"}), "choices": []})

    with pytest.raises(ModelUnavailable, match="EmptyResponse"):
        _model(handler).complete([{"role": "user", "content": "hi"}])


def test_parse_json_object_skips_braces_in_prose():
    assert parse_json_object('Format {cause}: {"a": 1}') == {"a": 1}
    assert parse_json_object('{"a": 1}\nNote: use {placeholder}') == {"a": 1}


def test_as_message_sends_valid_json_when_raw_arguments_are_invalid():
    message = ModelReply("", [ToolCall("c", "events", {"x": 1}, "not json")]).as_message()
    assert message["tool_calls"][0]["function"]["arguments"] == '{"x": 1}'


def test_default_client_ignores_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    model = OpenAIChatModel("https://api.z.ai/api/coding/paas/v4", "zai-key", "glm-5.3")
    http = model._client._client
    assert http._trust_env is False
    assert http._mounts == {}

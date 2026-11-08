"""The chat model behind one small interface, so tests can script it.

The real adapter uses the `openai` SDK against Z.ai's OpenAI-compatible endpoint. `complete`
takes a per-call `timeout` and `max_retries`; the graph passes max_retries=0 and does its own
single retry, within its deadline, for the failures the adapter marks `retryable` (timeout,
connection error, 429, 5xx). The constructor's max_retries=1 only serves direct callers such as
the manual eval. Structured output is JSON in the message text, parsed and
validated by the caller; the Z.ai endpoint's `response_format` support is not relied on."""

import json
from dataclasses import dataclass, field
from typing import Protocol

import httpx2
import openai


class ModelUnavailable(Exception):
    """The model call failed. `retryable` marks transient failures (timeout, connection error, 429, 5xx);
    `retry_after` is the server's Retry-After in seconds, when it sent one."""

    def __init__(self, message: str = "", *, retryable: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class ModelTimeout(ModelUnavailable):
    """One model call ran past its per-call timeout."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str = "{}"


@dataclass(frozen=True)
class ModelReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def as_message(self) -> dict:
        message: dict = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": _arguments_json(c)}}
                for c in self.tool_calls
            ]
        return message


def _arguments_json(call: ToolCall) -> str:
    """The arguments as valid JSON for the transcript: the model's own text when it parses, else re-encoded."""
    try:
        json.loads(call.raw_arguments)
    except (json.JSONDecodeError, TypeError):
        return json.dumps(call.arguments)
    return call.raw_arguments


class ChatModel(Protocol):
    """`timeout` (seconds) caps this call, and `max_retries` overrides the SDK retry count for it.
    None keeps the adapter's defaults."""

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> ModelReply: ...


class OpenAIChatModel:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 40,
        max_retries: int = 1,
        http_client: httpx2.Client | None = None,
    ) -> None:
        kwargs: dict = {"base_url": base_url, "api_key": api_key, "timeout": timeout, "max_retries": max_retries}
        # The client carries the model API key: never let proxy environment variables reroute it.
        kwargs["http_client"] = http_client if http_client is not None else openai.DefaultHttpxClient(trust_env=False)
        self._client = openai.OpenAI(**kwargs)
        self._model = model

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> ModelReply:
        kwargs: dict = {"model": self._model, "messages": messages, "temperature": 0.2}
        if tools:
            kwargs["tools"] = tools
        options = {k: v for k, v in (("timeout", timeout), ("max_retries", max_retries)) if v is not None}
        client = self._client.with_options(**options) if options else self._client
        try:
            response = client.chat.completions.create(**kwargs)
        except openai.APITimeoutError as exc:
            raise ModelTimeout(type(exc).__name__, retryable=True) from exc
        except openai.APIConnectionError as exc:
            raise ModelUnavailable(type(exc).__name__, retryable=True) from exc
        except openai.APIStatusError as exc:
            status = exc.status_code
            raise ModelUnavailable(
                type(exc).__name__,
                retryable=status == 429 or status >= 500,
                retry_after=_retry_after(exc.response.headers),
            ) from exc
        except openai.APIError as exc:
            raise ModelUnavailable(type(exc).__name__) from exc
        if not response.choices:
            raise ModelUnavailable("EmptyResponse")
        message = response.choices[0].message
        calls = []
        for call in message.tool_calls or []:
            raw = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                arguments = {}
            calls.append(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=arguments if isinstance(arguments, dict) else {},
                    raw_arguments=raw,
                )
            )
        usage = response.usage
        return ModelReply(
            content=message.content,
            tool_calls=calls,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )


def _retry_after(headers) -> float | None:
    """The server's Retry-After (retry-after-ms, or whole seconds) in seconds; HTTP dates are ignored."""
    for name, scale in (("retry-after-ms", 1000), ("retry-after", 1)):
        try:
            return max(0.0, float(headers[name]) / scale)
        except (KeyError, ValueError):
            continue
    return None


def parse_json_object(text: str | None) -> dict:
    """Parse the first JSON object in a model reply, tolerating ```json fences and prose around it."""
    if not text:
        raise ValueError("empty reply")
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("no JSON object in reply")

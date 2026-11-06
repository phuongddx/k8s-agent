"""The chat model behind one small interface, so tests can script it.

The real adapter uses the `openai` SDK against Z.ai's OpenAI-compatible endpoint. The SDK's
own retry (max_retries=1, exponential backoff) gives the spec's one retry on timeouts,
connection errors, 429 and 5xx. Structured output is JSON in the message text, parsed and
validated by the caller; the Z.ai endpoint's `response_format` support is not relied on."""

import json
from dataclasses import dataclass, field
from typing import Protocol

import httpx2
import openai


class ModelUnavailable(Exception):
    """The model failed after the retry."""


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
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.raw_arguments}}
                for c in self.tool_calls
            ]
        return message


class ChatModel(Protocol):
    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> ModelReply: ...


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
        if http_client is not None:
            kwargs["http_client"] = http_client
        self._client = openai.OpenAI(**kwargs)
        self._model = model

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> ModelReply:
        kwargs: dict = {"model": self._model, "messages": messages, "temperature": 0.2}
        if tools:
            kwargs["tools"] = tools
        try:
            response = self._client.chat.completions.create(**kwargs)
        except openai.APIError as exc:
            raise ModelUnavailable(type(exc).__name__) from exc
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


def parse_json_object(text: str | None) -> dict:
    """Parse the first JSON object in a model reply, tolerating ```json fences and prose around it."""
    if not text:
        raise ValueError("empty reply")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("reply is not a JSON object")
    return value

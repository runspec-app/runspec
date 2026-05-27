"""pip install runspec-console[openai]"""

from __future__ import annotations

from typing import Any

import openai as _openai

from .base import ChatResponse, ModelAdapter, ToolCall

DEFAULT_MODEL = "gpt-4o"
DEFAULT_SYSTEM = (
    "You are a helpful assistant with access to runspec tools running on local and remote hosts. "
    "Use tools when they help answer the user's request. "
    "When you call a tool, briefly explain what you're doing before the result."
)


def _to_openai_function(t: dict[str, Any]) -> dict[str, Any]:
    """Convert Anthropic-format tool (input_schema) to OpenAI function format (parameters)."""
    func: dict[str, Any] = {"name": t["name"]}
    if t.get("description"):
        func["description"] = t["description"]
    func["parameters"] = t.get("input_schema") or t.get("parameters") or {
        "type": "object", "properties": {}
    }
    return func


class OpenAIAdapter(ModelAdapter):
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        system: str = DEFAULT_SYSTEM,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.client = _openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.system = system

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResponse:
        system_msg = {"role": "system", "content": self.system}
        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=[system_msg, *messages],
        )
        if tools:
            kwargs["tools"] = [{"type": "function", "function": _to_openai_function(t)} for t in tools]
            kwargs["tool_choice"] = "auto"
        response = await self.client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        text = msg.content
        tool_calls = []
        if msg.tool_calls:
            import json
            tool_calls = [
                ToolCall(id=tc.id, name=tc.function.name, input=json.loads(tc.function.arguments))
                for tc in msg.tool_calls
            ]
        stop = response.choices[0].finish_reason or "stop"
        return ChatResponse(text=text, tool_calls=tool_calls, stop_reason=stop, _raw=response)

    async def stream_chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):  # type: ignore[override]
        system_msg = {"role": "system", "content": self.system}
        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=[system_msg, *messages],
            stream=True,
        )
        if tools:
            kwargs["tools"] = [{"type": "function", "function": _to_openai_function(t)} for t in tools]
            kwargs["tool_choice"] = "auto"
        async for chunk in await self.client.chat.completions.create(**kwargs):
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def stream_with_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):  # type: ignore[override]
        # Fall back to non-streaming chat() — accumulating streaming deltas for tool calls
        # requires complex state management; non-streaming is simpler and correct for tool turns.
        response = await self.chat(messages, tools)
        if response.text:
            yield ("text", response.text)
        yield ("done", response)

    def make_tool_turn(
        self, response: ChatResponse, results: list[tuple[ToolCall, str]]
    ) -> list[dict[str, Any]]:
        raw_msg = response._raw.choices[0].message
        turns: list[dict[str, Any]] = [{"role": "assistant", "content": raw_msg.content, "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": str(tc.input)}}
            for tc, _ in results
        ]}]
        for tc, result in results:
            turns.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        return turns

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
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
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

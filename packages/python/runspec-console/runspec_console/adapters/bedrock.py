"""
pip install runspec-console[bedrock]

Uses anthropic[bedrock] — Claude models via AWS Bedrock.

Standard auth: AWS credential chain (env vars, ~/.aws/credentials, IAM role).
Custom proxy auth: set base_url + api_key (HTTP Basic token) for a corporate
Bedrock proxy — the anthropic SDK passes api_key as the Authorization header.
"""

from __future__ import annotations

from typing import Any

import anthropic

from .base import ChatResponse, ModelAdapter, ToolCall

DEFAULT_MODEL = "anthropic.claude-sonnet-4-6"
DEFAULT_SYSTEM = (
    "You are a helpful assistant with access to runspec tools running on local and remote hosts. "
    "Use tools when they help answer the user's request. "
    "When you call a tool, briefly explain what you're doing before the result."
)


class BedrockAdapter(ModelAdapter):
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        system: str = DEFAULT_SYSTEM,
        aws_region: str | None = None,
        aws_access_key: str | None = None,
        aws_secret_key: str | None = None,
        aws_session_token: str | None = None,
        # Corporate proxy path: supply base_url + token instead of AWS creds
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self.system = system
        if base_url:
            # Corporate proxy — treat as a regular Anthropic-compatible endpoint
            self.client: anthropic.AsyncAnthropic | anthropic.AsyncAnthropicBedrock = (
                anthropic.AsyncAnthropic(base_url=base_url, api_key=api_key or "unused")
            )
        else:
            kwargs: dict[str, Any] = {}
            if aws_region:
                kwargs["aws_region"] = aws_region
            if aws_access_key:
                kwargs["aws_access_key"] = aws_access_key
            if aws_secret_key:
                kwargs["aws_secret_key"] = aws_secret_key
            if aws_session_token:
                kwargs["aws_session_token"] = aws_session_token
            self.client = anthropic.AsyncAnthropicBedrock(**kwargs)

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ChatResponse:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=4096,
            messages=messages,
            system=self.system,
        )
        if tools:
            kwargs["tools"] = tools
        response = await self.client.messages.create(**kwargs)
        text = next(
            (block.text for block in response.content if hasattr(block, "text")), None
        )
        tool_calls = [
            ToolCall(id=block.id, name=block.name, input=block.input)
            for block in response.content
            if block.type == "tool_use"
        ]
        return ChatResponse(text=text, tool_calls=tool_calls, stop_reason=response.stop_reason, _raw=response)

    async def stream_chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):  # type: ignore[override]
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=4096,
            messages=messages,
            system=self.system,
        )
        if tools:
            kwargs["tools"] = tools
        async with self.client.messages.stream(**kwargs) as stream:
            async for token in stream.text_stream:
                yield token

    async def stream_with_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):  # type: ignore[override]
        import json
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=4096,
            messages=messages,
            system=self.system,
        )
        if tools:
            kwargs["tools"] = tools
        tool_map: dict[int, dict[str, Any]] = {}
        stop_reason = "end_turn"
        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                ev_type = getattr(event, "type", None)
                if ev_type == "content_block_start":
                    cb = getattr(event, "content_block", None)
                    if cb and getattr(cb, "type", None) == "tool_use":
                        tool_map[event.index] = {"id": cb.id, "name": cb.name, "json": ""}
                elif ev_type == "content_block_delta":
                    d = getattr(event, "delta", None)
                    if d:
                        if getattr(d, "type", None) == "text_delta":
                            yield ("text", d.text)
                        elif getattr(d, "type", None) == "input_json_delta":
                            if event.index in tool_map:
                                tool_map[event.index]["json"] += d.partial_json
                elif ev_type == "message_delta":
                    d = getattr(event, "delta", None)
                    if d:
                        stop_reason = getattr(d, "stop_reason", stop_reason) or stop_reason
            final = await stream.get_final_message()
        tool_calls = []
        for tc in tool_map.values():
            try:
                inp = json.loads(tc["json"]) if tc["json"] else {}
            except Exception:
                inp = {}
            tool_calls.append(ToolCall(id=tc["id"], name=tc["name"], input=inp))
        yield ("done", ChatResponse(text=None, tool_calls=tool_calls, stop_reason=stop_reason, _raw=final))

    def make_tool_turn(
        self, response: ChatResponse, results: list[tuple[ToolCall, str]]
    ) -> list[dict[str, Any]]:
        return [
            {"role": "assistant", "content": response._raw.content},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tc.id, "content": result}
                    for tc, result in results
                ],
            },
        ]

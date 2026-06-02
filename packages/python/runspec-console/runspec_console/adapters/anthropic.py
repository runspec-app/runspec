"""pip install runspec-console[anthropic]"""

from __future__ import annotations

import subprocess
import time
from typing import Any

import anthropic

from .base import ChatResponse, ModelAdapter, ToolCall, apply_prompt_caching

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_SYSTEM = (
    "You are a helpful assistant with access to runspec tools running on local and remote hosts. "
    "Use tools when they help answer the user's request. "
    "When you call a tool, briefly explain what you're doing before the result."
)


class AnthropicAdapter(ModelAdapter):
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        system: str = DEFAULT_SYSTEM,
        api_key: str | None = None,
        base_url: str | None = None,
        api_key_command: str | None = None,
        api_key_ttl_ms: int = 0,
    ) -> None:
        """
        Args:
            api_key: Static API key. Used directly unless api_key_command is set.
            base_url: Override the Anthropic API endpoint (e.g. a corporate AI proxy).
            api_key_command: Shell command whose stdout (stripped) is used as the
                API key. Mirrors Claude Code's apiKeyHelper. When set, api_key is
                ignored and the client is (re)built from the command's output.
            api_key_ttl_ms: How long to cache the command's result before re-running
                it. 0 re-runs the command on every request.
        """
        self.model = model
        self.system = system
        self._base_url = base_url
        self._api_key_command = api_key_command
        self._api_key_ttl_s = api_key_ttl_ms / 1000.0
        self._cached_key: str | None = None
        self._key_fetched_at: float = 0.0

        # anthropic is an optional, untyped dependency here, so the client is Any.
        self.client: Any
        if api_key_command:
            # Built lazily on the first request via _refresh_key_if_needed.
            self.client = None
        else:
            self.client = anthropic.AsyncAnthropic(**self._client_kwargs(api_key))

    def _client_kwargs(self, api_key: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"api_key": api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return kwargs

    def _refresh_key_if_needed(self) -> None:
        """Re-run api_key_command and rebuild the client when the cache is stale.

        No-op when no command is configured (static-key path)."""
        if not self._api_key_command:
            return
        now = time.monotonic()
        if (
            self._cached_key is not None
            and self._api_key_ttl_s > 0
            and (now - self._key_fetched_at) < self._api_key_ttl_s
        ):
            return  # cached key still valid
        result = subprocess.run(
            self._api_key_command,
            capture_output=True,
            text=True,
            shell=True,
        )
        key = result.stdout.strip()
        if not key:
            raise RuntimeError(
                f"api_key_command returned no output (exit {result.returncode}): {result.stderr.strip()}"
            )
        self._cached_key = key
        self._key_fetched_at = now
        self.client = anthropic.AsyncAnthropic(**self._client_kwargs(key))

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ChatResponse:
        self._refresh_key_if_needed()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=4096,
            messages=messages,
            system=self.system,
        )
        if tools:
            kwargs["tools"] = tools
        apply_prompt_caching(kwargs)
        response = await self.client.messages.create(**kwargs)
        text = next(
            (block.text for block in response.content if hasattr(block, "text")), None
        )
        tool_calls = [
            ToolCall(id=block.id, name=block.name, input=block.input)
            for block in response.content
            if block.type == "tool_use"
        ]
        return ChatResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
            _raw=response,
        )

    async def stream_chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ):  # type: ignore[override]
        self._refresh_key_if_needed()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=4096,
            messages=messages,
            system=self.system,
        )
        if tools:
            kwargs["tools"] = tools
        apply_prompt_caching(kwargs)
        async with self.client.messages.stream(**kwargs) as stream:
            async for token in stream.text_stream:
                yield token

    async def stream_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ):  # type: ignore[override]
        import json

        self._refresh_key_if_needed()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=4096,
            messages=messages,
            system=self.system,
        )
        if tools:
            kwargs["tools"] = tools
        apply_prompt_caching(kwargs)
        # Collect tool input JSON chunks indexed by content-block position
        tool_map: dict[int, dict[str, Any]] = {}
        stop_reason = "end_turn"
        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                ev_type = getattr(event, "type", None)
                if ev_type == "content_block_start":
                    cb = getattr(event, "content_block", None)
                    if cb and getattr(cb, "type", None) == "tool_use":
                        tool_map[event.index] = {
                            "id": cb.id,
                            "name": cb.name,
                            "json": "",
                        }
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
                        stop_reason = (
                            getattr(d, "stop_reason", stop_reason) or stop_reason
                        )
            final = await stream.get_final_message()
        tool_calls = []
        for tc in tool_map.values():
            try:
                inp = json.loads(tc["json"]) if tc["json"] else {}
            except Exception:
                inp = {}
            tool_calls.append(ToolCall(id=tc["id"], name=tc["name"], input=inp))
        yield (
            "done",
            ChatResponse(
                text=None, tool_calls=tool_calls, stop_reason=stop_reason, _raw=final
            ),
        )

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

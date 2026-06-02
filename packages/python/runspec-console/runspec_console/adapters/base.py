"""
base.py — ModelAdapter ABC shared across all LLM providers.

Identical contract to runspec-chat's adapter.py so implementations
can be ported between the two packages without changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ChatResponse:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: str  # "tool_use" | "end_turn" | "stop"
    _raw: Any = field(repr=False, default=None)


class ModelAdapter(ABC):
    @abstractmethod
    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ChatResponse: ...

    @abstractmethod
    def stream_chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[str]:
        """Yield text tokens as they arrive from the model."""
        ...

    async def stream_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ):
        """
        Yield ('text', str) for each text token, then ('done', ChatResponse) at the end.

        Default falls back to non-streaming chat(). Override for true streaming with tools.
        """
        response = await self.chat(messages, tools)
        if response.text:
            yield ("text", response.text)
        yield ("done", response)

    @abstractmethod
    def make_tool_turn(
        self, response: ChatResponse, results: list[tuple[ToolCall, str]]
    ) -> list[dict[str, Any]]:
        """Return [assistant_turn, tool_result_turn] to append to the conversation."""
        ...


def apply_prompt_caching(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Add ephemeral cache_control breakpoints to the static prompt prefix.

    Prompt caching is a *prefix match* in render order ``tools → system →
    messages``: a breakpoint on the last tool caches the whole (large, static)
    tools block, and turning ``system`` into a cached text block caches
    tools+system together. The tools array is rebuilt from the same discovered
    runnables each turn, so it's byte-stable within a session — subsequent turns
    read the prefix at ~0.1x instead of full price. Below the model's minimum
    cacheable prefix (~2K tokens on Sonnet 4.6, ~4K on Opus/Haiku) the API
    silently skips caching with no error, so this is always safe to apply.

    Pure and SDK-free: takes the messages.create()/stream() kwargs dict, returns
    it with caching applied. Copies the tool dicts it annotates rather than
    mutating the caller's shared tool list.
    """
    tools = kwargs.get("tools")
    if tools:
        annotated = [dict(t) for t in tools]
        annotated[-1] = {**annotated[-1], "cache_control": {"type": "ephemeral"}}
        kwargs["tools"] = annotated
    system = kwargs.get("system")
    if isinstance(system, str) and system.strip():
        kwargs["system"] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    return kwargs


def load_adapter(provider: str, **kwargs: Any) -> ModelAdapter:
    """
    Instantiate the named adapter.  Raises ImportError with install instructions
    if the required extra is not installed.

    provider: "anthropic" | "openai" | "bedrock"
    kwargs:   passed straight through to the adapter __init__
    """
    if provider == "anthropic":
        try:
            from .anthropic import AnthropicAdapter

            return AnthropicAdapter(**kwargs)
        except ImportError:
            raise ImportError(
                "Install the Anthropic extra: pip install runspec-console[anthropic]"
            )
    if provider == "openai":
        try:
            from .openai import OpenAIAdapter

            return OpenAIAdapter(**kwargs)
        except ImportError:
            raise ImportError(
                "Install the OpenAI extra: pip install runspec-console[openai]"
            )
    if provider == "bedrock":
        try:
            from .bedrock import BedrockAdapter

            return BedrockAdapter(**kwargs)
        except ImportError:
            raise ImportError(
                "Install the Bedrock extra: pip install runspec-console[bedrock]"
            )
    raise ValueError(
        f"Unknown LLM provider: {provider!r}. Choose from: anthropic, openai, bedrock"
    )

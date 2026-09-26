"""pip install runspec-console[anthropic]"""

from __future__ import annotations

import time
from typing import Any

import anthropic

from .base import (
    ChatResponse,
    ModelAdapter,
    ModelCapabilities,
    ToolCall,
    anthropic_thinking_base,
    apply_prompt_caching,
    apply_thinking,
    capabilities_from_limits,
    effort_level,
    run_api_key_command,
)

# Sentinel for "the Models API lookup hasn't been attempted yet" — distinct from a
# cached None (attempted, returned nothing / unreachable, don't retry).
_UNSET: Any = object()

DEFAULT_MODEL = "claude-sonnet-4-6"
# Output-token ceiling per request. A ceiling, not a target — billed only when
# produced, so raising it costs nothing on short replies. The agentic loop
# auto-continues past it (bridge_turn `_run_agent_turn`), so this is a
# conservative default the operator can raise via `[llm] max_tokens`.
DEFAULT_MAX_TOKENS = 8192
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
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str = "",
        model_limits: dict[str, Any] | None = None,
        cache_ttl: str = "",
        fallbacks: str = "",
        thinking_display: str = "",
    ) -> None:
        """
        Args:
            api_key: Static API key. Used directly unless api_key_command is set.
            base_url: Override the Anthropic API endpoint (e.g. a corporate AI proxy).
            api_key_command: Shell command whose stdout (stripped) is used as the
                API key. Mirrors Claude Code's apiKeyHelper. When set, api_key is
                ignored and the client is (re)built from the command's output. Run
                with the current venv made discoverable (this venv's script dir on
                PATH, plus RUNSPEC_PYTHON / RUNSPEC_VENV) so a helper shipped into
                the venv resolves wherever the venv was created — see
                ``base.run_api_key_command``.
            api_key_ttl_ms: How long to cache the command's result before re-running
                it. 0 re-runs the command on every request.
            max_tokens: Output-token ceiling per request (``[llm] max_tokens``).
            effort: Reasoning effort (``[llm] effort``) — "" / "off" (default, no
                extended thinking), or "low" / "medium" / "high" / "xhigh" / "max".
                Enables Anthropic *adaptive* extended thinking (``thinking={"type":
                "adaptive"}`` + ``output_config={"effort": <level>}``); the model
                self-manages its thinking depth and it counts toward ``max_tokens``.
                See ``base.apply_thinking``.
            model_limits: The ``[llm.model_limits]`` table (model-id substring →
                capability fields) used by ``capabilities()``; this adapter also
                fills unset numeric limits from the Models API when reachable.
            cache_ttl: Prompt-cache lifetime (``[llm] cache_ttl``) — "" / "5m"
                (default) or "1h". The 1-hour cache survives an operator pausing
                at a confirm card; it costs more to write (2x vs 1.25x base).
            fallbacks: Refusal fallback (``[llm] fallbacks``) — "" (off) or
                "default": on a model whose safety classifiers can decline (Fable
                5.x, Opus 5.x) the API re-runs a declined request on Anthropic's
                recommended model in the same call. Claude API only (beta
                ``server-side-fallback-2026-07-01``); ignored on other models.
            thinking_display: ``thinking.display`` (``[llm] thinking_display``) —
                "" (the model's default, usually "omitted") or "summarized" to
                stream readable reasoning summaries, surfaced as ``("thinking",
                text)`` events. Only sent when thinking is on.
        """
        self.model = model
        self.system = system
        self.max_tokens = int(max_tokens) or DEFAULT_MAX_TOKENS
        self._effort = effort_level(effort)
        self._cache_ttl = "1h" if str(cache_ttl or "").strip().lower() == "1h" else ""
        self._fallbacks = (
            "default" if str(fallbacks or "").strip().lower() == "default" else ""
        )
        display = str(thinking_display or "").strip().lower()
        self._thinking_display = display if display in ("summarized", "omitted") else ""
        # [llm.model_limits] table for the default capabilities() lookup, plus a
        # cache of the (best-effort) Models API enrichment — see capabilities().
        self._model_limits = model_limits or {}
        self._model_info_cache: Any = _UNSET
        self._base_url = base_url
        self._static_api_key = api_key
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
        key = run_api_key_command(self._api_key_command)
        self._cached_key = key
        self._key_fetched_at = now
        self.client = anthropic.AsyncAnthropic(**self._client_kwargs(key))

    def capabilities(self) -> ModelCapabilities:
        """Model capabilities: the ``[llm.model_limits]`` table, with any numeric
        limit it leaves unset filled best-effort from the Anthropic **Models API**
        (``client.models.retrieve``) when that endpoint is reachable.

        The table is authoritative (an operator override wins); the Models API only
        fills gaps, and a lookup failure — the common case behind a corporate proxy
        where the Models API is blocked — is cached so it's attempted at most once
        and silently falls back to the table. The booleans come from the built-in
        per-family thinking defaults (``anthropic_thinking_base``), which the table
        overrides — the Models API doesn't report them."""
        caps = self._base_caps()
        if caps.context_window is None or caps.max_output_tokens is None:
            info = self._model_api_info()
            if info:
                if caps.context_window is None and info.get("context_window"):
                    caps.context_window = int(info["context_window"])
                if caps.max_output_tokens is None and info.get("max_output_tokens"):
                    caps.max_output_tokens = int(info["max_output_tokens"])
        return caps

    def _base_caps(self) -> ModelCapabilities:
        """Capabilities from the ``[llm.model_limits]`` table + the built-in
        per-family thinking defaults — **without** the Models API lookup, so it's
        cheap enough to call on every request. This is what ``apply_thinking``
        needs (the thinking booleans never come from the Models API); the public
        ``capabilities()`` enriches this with the numeric limits."""
        return capabilities_from_limits(
            self.model, self._model_limits, base=anthropic_thinking_base(self.model)
        )

    def _model_api_info(self) -> dict[str, Any] | None:
        """Best-effort ``{context_window, max_output_tokens}`` from the Models API,
        cached (including a failed/empty lookup, so it never retries or blocks a
        later turn). A synchronous, short-timeout call — the Models API may be
        unreachable (corporate proxy), so any exception falls back to ``None``."""
        if self._model_info_cache is not _UNSET:
            return self._model_info_cache
        self._model_info_cache = None
        try:
            key = self._static_api_key
            if self._api_key_command:
                key = self._cached_key
                if key is None:
                    key = run_api_key_command(self._api_key_command)
                    self._cached_key = key
                    self._key_fetched_at = time.monotonic()
            kwargs = self._client_kwargs(key)
            kwargs["timeout"] = 5.0
            sync_client = anthropic.Anthropic(**kwargs)
            info = sync_client.models.retrieve(self.model)
            self._model_info_cache = {
                "context_window": getattr(info, "context_window", None),
                "max_output_tokens": getattr(info, "max_output_tokens", None),
            }
        except Exception:  # noqa: BLE001 — Models API is optional; table is the floor
            self._model_info_cache = None
        return self._model_info_cache

    def _shape_request(self, kwargs: dict[str, Any]) -> None:
        """Apply thinking/effort and prompt caching, then this adapter's opt-in
        extras (``cache_ttl``, ``thinking_display``, ``fallbacks``). The extras
        live here, not in the shared ``base`` helpers, so the bedrock / langserve
        built-ins and plugin adapters are unaffected."""
        apply_thinking(kwargs, self._effort, self._base_caps(), self.model)
        apply_prompt_caching(kwargs)
        if self._cache_ttl:
            _set_cache_ttl(kwargs, self._cache_ttl)
        thinking = kwargs.get("thinking")
        if (
            self._thinking_display
            and isinstance(thinking, dict)
            and thinking.get("type") == "adaptive"
        ):
            kwargs["thinking"] = {**thinking, "display": self._thinking_display}
        if self._fallbacks and _supports_fallbacks(self.model):
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["extra_body"] = {"fallbacks": self._fallbacks}

    def _messages_api(self, kwargs: dict[str, Any]) -> Any:
        """The beta namespace when a beta is requested (fallbacks), else the
        stable one — ``betas`` is only accepted under ``client.beta``."""
        return self.client.beta.messages if "betas" in kwargs else self.client.messages

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ChatResponse:
        self._refresh_key_if_needed()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=messages,
            system=self.system,
        )
        if tools:
            kwargs["tools"] = tools
        self._shape_request(kwargs)
        response = await self._messages_api(kwargs).create(**kwargs)
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
            max_tokens=self.max_tokens,
            messages=messages,
            system=self.system,
        )
        if tools:
            kwargs["tools"] = tools
        self._shape_request(kwargs)
        async with self._messages_api(kwargs).stream(**kwargs) as stream:
            async for token in stream.text_stream:
                yield token

    async def stream_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ):  # type: ignore[override]
        import json

        self._refresh_key_if_needed()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=messages,
            system=self.system,
        )
        if tools:
            kwargs["tools"] = tools
        self._shape_request(kwargs)
        # Collect tool input JSON chunks indexed by content-block position
        tool_map: dict[int, dict[str, Any]] = {}
        stop_reason = "end_turn"
        async with self._messages_api(kwargs).stream(**kwargs) as stream:
            async for event in stream:
                ev_type = getattr(event, "type", None)
                if ev_type == "content_block_start":
                    cb = getattr(event, "content_block", None)
                    if cb and getattr(cb, "type", None) == "fallback":
                        # A refusal fallback: tool calls from the declined
                        # attempt must not run (and aren't echoed back).
                        tool_map.clear()
                    elif cb and getattr(cb, "type", None) == "tool_use":
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
                        elif getattr(d, "type", None) == "thinking_delta":
                            if getattr(d, "thinking", ""):
                                yield ("thinking", d.thinking)
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
            {"role": "assistant", "content": _echo_content(response._raw.content)},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tc.id, "content": result}
                    for tc, result in results
                ],
            },
        ]


# ── opt-in request extras (see AnthropicAdapter._shape_request) ────────────────

_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Model families whose safety classifiers can decline a request and which accept
# server-side fallbacks. Matched as a substring so dated / Bedrock-style ids work;
# every other model is sent no fallbacks field (it would be rejected).
_FALLBACK_MODEL_FAMILIES = ("claude-fable-5", "claude-opus-5", "claude-mythos-5")


def _supports_fallbacks(model: str) -> bool:
    m = (model or "").lower()
    return any(family in m for family in _FALLBACK_MODEL_FAMILIES)


def _with_ttl(block: Any, ttl: str) -> Any:
    if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
        return {**block, "cache_control": {**block["cache_control"], "ttl": ttl}}
    return block


def _set_cache_ttl(kwargs: dict[str, Any], ttl: str) -> None:
    """Give every cache breakpoint ``apply_prompt_caching`` placed the same TTL.

    Rebuilds only the lists holding a marker (tools, system blocks, and the
    marked message's content), so the caller's history is never mutated. One TTL
    for all breakpoints also satisfies the API rule that a longer TTL may not
    follow a shorter one."""
    if isinstance(kwargs.get("tools"), list):
        kwargs["tools"] = [_with_ttl(t, ttl) for t in kwargs["tools"]]
    if isinstance(kwargs.get("system"), list):
        kwargs["system"] = [_with_ttl(b, ttl) for b in kwargs["system"]]
    messages = kwargs.get("messages")
    if isinstance(messages, list):
        out = []
        for msg in messages:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list) and any(
                isinstance(b, dict) and "cache_control" in b for b in content
            ):
                msg = {**msg, "content": [_with_ttl(b, ttl) for b in content]}
            out.append(msg)
        kwargs["messages"] = out


# Block types from a declined attempt that must not be echoed back after a
# mid-output fallback (text before the boundary is fine to keep).
_PRE_FALLBACK_DROP = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}


def _echo_content(content: Any) -> Any:
    """The assistant content to send back next turn.

    Unchanged unless a refusal fallback happened mid-output: then everything the
    declined model produced before the last ``fallback`` block except its text is
    dropped, as the API requires."""
    if not isinstance(content, list):
        return content
    types = [
        getattr(b, "type", None) or (b.get("type") if isinstance(b, dict) else None)
        for b in content
    ]
    if "fallback" not in types:
        return content
    boundary = len(types) - 1 - types[::-1].index("fallback")
    return [
        b
        for i, (b, t) in enumerate(zip(content, types, strict=True))
        if i >= boundary or t == "text"
    ]

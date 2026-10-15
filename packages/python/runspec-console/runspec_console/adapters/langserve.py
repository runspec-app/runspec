"""
pip install runspec-console[langserve]

Talk to a LangServe ``/invoke`` endpoint — a LangChain Runnable published over
HTTP. Use this when your only model access is a corporate LLM gateway that
exposes a LangServe route (commonly Bedrock/Claude underneath) rather than a
native Anthropic/OpenAI API.

Wire format
-----------
A LangServe Runnable answers ``POST {base_url}/invoke`` with a body of
``{"input": ..., "config": ..., "kwargs": ...}`` and returns
``{"output": ..., "metadata": ...}``. The *shapes* of ``input`` and ``output``
are defined by the chain the gateway published, so the two parts most likely to
vary per-gateway are configurable:

* messages are sent as LangChain-ingestable role dicts (``convert_to_messages``
  accepts the OpenAI ``{"role", "content"}`` form natively, including
  ``tool_calls`` and ``{"role": "tool"}`` results);
* tools are sent as OpenAI-function dicts (the lingua franca ``bind_tools``
  understands).

By default both go inside the ``input`` object::

    {"input": {"messages": [...], "tools": [...]}, "config": {}, "kwargs": {}}

Knobs (set under ``[llm]`` in the config): ``input_messages_key`` (default
``"messages"``), ``input_tools_key`` (default ``"tools"``), and
``tools_in_config`` — when true, tools move to ``config.configurable.<key>``
instead of the input object (for chains that accept tools as a configurable
field). Adjust these once you've seen the gateway's ``GET /input_schema``.

``max_tokens`` forwards an output-token ceiling to the gateway (0 = don't send;
the published chain usually pins its own). ``max_tokens_in`` picks the slot —
``"kwargs"`` (default), ``"config"`` (``config.configurable.<key>``, only for a
chain that declares it), or ``"input"`` — and ``max_tokens_key`` renames the
field. The gateway's reported stop reason (``stop_reason`` / ``stopReason`` /
``finish_reason`` in the message metadata) is surfaced so the agentic loop can
auto-continue a reply the gateway truncated at that ceiling.

``effort`` (``[llm] effort``, reasoning effort) is sent only when set. Since this
adapter speaks the OpenAI wire shape, the default is a plain
``reasoning_effort: "<level>"`` string; ``effort_key`` / ``effort_in`` rename the
field / pick the slot (same three choices as ``max_tokens_in``). It's a
pass-through string — the level and field are whatever your chain accepts, *not*
the Anthropic ``thinking``/``output_config`` shape. A gateway that wants that
native shape overrides ``_build_payload`` (see the comment on that method).

Auth
----
Bearer token in the ``Authorization`` header, taken from a static ``api_key`` or
— for short-lived corporate tokens — an ``api_key_command`` re-run on a TTL
(same vending pattern as the Anthropic adapter). ``auth_header`` / ``auth_scheme``
override the header name and scheme.

Tool calling is structured: the returned ``AIMessage`` carries a ``tool_calls``
array which maps straight onto runspec tool calls.

Streaming (``[llm] stream_events``, default on): ``stream_with_tools`` drives the
LangServe ``/stream_events`` SSE route, yielding text deltas as they arrive and
ending with a ``ChatResponse`` carrying the gateway's real stop reason + usage +
tool calls (from the terminal ``on_chat_model_end`` event) — so a long reply no
longer rides one long-lived ``/invoke`` request. Falls back to ``/invoke`` (and
disables streaming for the session) if the route is unavailable, or when
``stream_events = false``.

Prompt caching (``[llm] cache_markers``, default off): for a gateway that forwards
Anthropic ``cache_control`` to the model, mark the system prompt, the last tool,
and the last message block each turn; the tool list is serialised deterministically
(sorted) so the cached prefix is byte-stable across turns. ``parse_output`` reads
``cache_creation`` as well as ``cache_read`` from ``usage_metadata``.

Model limits (``[llm.model_limits]``): the gateway hides the model, so its limits
(context window, output ceiling, whether it accepts thinking/effort) are declared
in config and read by ``capabilities()`` — see ``base.capabilities_from_limits``.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

from .base import (
    ChatResponse,
    ModelAdapter,
    ToolCall,
    _OUTPUT_TRUNCATION_STOPS,
    run_api_key_command,
)

DEFAULT_MODEL = ""  # the published chain usually pins its own model
DEFAULT_SYSTEM = (
    "You are a helpful assistant with access to runspec tools running on local and remote hosts. "
    "Use tools when they help answer the user's request. "
    "When you call a tool, briefly explain what you're doing before the result."
)


# ── pure mapping helpers (no httpx — unit-testable without the extra) ──────────


def _to_openai_tool(t: dict[str, Any]) -> dict[str, Any]:
    """Anthropic-format runspec tool (``input_schema``) → OpenAI function tool."""
    func: dict[str, Any] = {"name": t["name"]}
    if t.get("description"):
        func["description"] = t["description"]
    func["parameters"] = (
        t.get("input_schema")
        or t.get("parameters")
        or {"type": "object", "properties": {}}
    )
    return {"type": "function", "function": func}


def _place_in_slot(payload: dict[str, Any], slot: str, key: str, value: Any) -> None:
    """Put ``{key: value}`` into one of the three ``/invoke`` body slots.

    ``slot`` is ``"config"`` (``config.configurable.<key>`` — only for a chain
    that *declares* that configurable field, else LangChain rejects it),
    ``"input"`` (alongside the messages), or ``"kwargs"`` (the protocol's
    dedicated extra-args bucket — the safe default for a value the chain forwards
    to the model). Shared by ``max_tokens`` and ``effort`` so they land
    identically.
    """
    if slot == "config":
        payload["config"].setdefault("configurable", {})[key] = value
    elif slot == "input":
        payload["input"][key] = value
    else:  # "kwargs"
        payload["kwargs"][key] = value


_EPHEMERAL = {"type": "ephemeral"}


def _as_text_blocks(content: Any) -> list[dict[str, Any]]:
    """Normalise a message/system ``content`` to a list of typed text blocks, so a
    trailing ``cache_control`` marker can be attached to the last one. A string
    becomes one text block; an existing block list is copied (shallow)."""
    if isinstance(content, list):
        return [
            dict(b) if isinstance(b, dict) else {"type": "text", "text": str(b)}
            for b in content
        ]
    return [{"type": "text", "text": "" if content is None else str(content)}]


def _mark_last_block(content: Any) -> list[dict[str, Any]]:
    """Return ``content`` as text blocks with an ephemeral ``cache_control`` marker
    on the **last** block — the prompt-cache breakpoint for a message."""
    blocks = _as_text_blocks(content)
    if blocks:
        blocks[-1] = {**blocks[-1], "cache_control": _EPHEMERAL}
    return blocks


def build_payload(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    system: str,
    *,
    messages_key: str = "messages",
    tools_key: str = "tools",
    tools_in_config: bool = False,
    max_tokens: int = 0,
    max_tokens_key: str = "max_tokens",
    max_tokens_in: str = "kwargs",
    effort: str = "",
    effort_key: str = "reasoning_effort",
    effort_in: str = "kwargs",
    cache_markers: bool = False,
) -> dict[str, Any]:
    """Assemble the ``/invoke`` request body.

    The system prompt is prepended as a ``system`` role message — LangChain maps
    it to a ``SystemMessage`` — so it survives even when the chain doesn't expose
    a separate system slot.

    ``max_tokens`` is sent only when > 0 (the published chain usually pins its
    own, so it's off by default). Where it lands is gateway-specific, so
    ``max_tokens_in`` selects the slot: ``"kwargs"`` (the LangServe extra-args
    bucket, default), ``"config"`` (``config.configurable.<key>`` — only for a
    chain that *declares* that configurable field, else LangChain rejects it), or
    ``"input"``. ``max_tokens_key`` renames the field.

    ``effort`` (reasoning effort) is sent only when non-empty. Because this
    adapter speaks the OpenAI wire shape (tools are OpenAI-function dicts), the
    default is to send the plain level string under an OpenAI-style
    ``reasoning_effort`` field — ``effort_key`` renames it, ``effort_in`` picks
    the slot (same three choices as ``max_tokens_in``). This is deliberately a
    *pass-through string* (``"low"``/``"high"``/…), not the Anthropic
    ``thinking``/``output_config`` shape: the value and field are whatever your
    gateway's chain accepts. If your gateway instead wants the native Anthropic
    *adaptive* thinking shape, don't use ``effort_key``/``effort_in`` — override
    ``LangServeAdapter._build_payload`` and inject it directly (see that method's
    comment, which shows the ``apply_thinking`` one-liner).

    ``cache_markers`` (off by default) adds Anthropic-style ephemeral
    ``cache_control`` breakpoints so a gateway that forwards them (a proxy in front
    of Bedrock/Anthropic) caches the static prefix: the system prompt becomes a
    text *block* carrying the marker, the **last** tool carries it (caching the
    whole tools block, which renders before the messages), and the **last content
    block of the last message** carries it each turn. To keep the cached prefix
    byte-stable across turns, the tools are emitted **sorted by name** and every
    annotated dict is a copy (the caller's shared lists are never mutated). The
    cacheable prefix (system + tools) can be extracted deterministically with
    :func:`cacheable_prefix`.
    """
    wire_messages: list[dict[str, Any]] = []
    if system.strip():
        if cache_markers:
            wire_messages.append(
                {"role": "system", "content": _mark_last_block(system)}
            )
        else:
            wire_messages.append({"role": "system", "content": system})
    wire_messages.extend(messages)

    oai_tools = [_to_openai_tool(t) for t in tools] if tools else []
    if cache_markers and oai_tools:
        # Deterministic order so the cached tools prefix is byte-identical across
        # turns (discovery can reorder the tool list). Sort by function name;
        # fall back to the JSON form for a tool with no name (never in practice).
        oai_tools = sorted(
            oai_tools,
            key=lambda t: (
                (t.get("function", {}) or {}).get("name", "")
                or json.dumps(t, sort_keys=True)
            ),
        )
        # Breakpoint on the last (now stable-ordered) tool — caches the whole block.
        oai_tools[-1] = {**oai_tools[-1], "cache_control": _EPHEMERAL}

    payload: dict[str, Any] = {
        "input": {messages_key: wire_messages},
        "config": {},
        "kwargs": {},
    }
    # Per-turn breakpoint: mark the last content block of the last message, so the
    # growing conversation prefix up to the previous turn stays cacheable.
    if cache_markers and messages:
        last = dict(wire_messages[-1])
        last["content"] = _mark_last_block(last.get("content"))
        wire_messages[-1] = last
    if oai_tools:
        if tools_in_config:
            payload["config"] = {"configurable": {tools_key: oai_tools}}
        else:
            payload["input"][tools_key] = oai_tools
    if max_tokens and int(max_tokens) > 0 and max_tokens_key:
        _place_in_slot(payload, max_tokens_in, max_tokens_key, int(max_tokens))
    if effort and effort_key:
        _place_in_slot(payload, effort_in, effort_key, effort)
    return payload


def cacheable_prefix(payload: dict[str, Any], *, tools_key: str = "tools") -> str:
    """Deterministic serialisation of the **cacheable prefix** of a ``/invoke``
    payload — the system message plus the tools block, the portion prompt caching
    matches on (tools → system render first). Serialised with ``sort_keys`` so two
    turns with the same tools+system produce a byte-identical string regardless of
    the conversation messages that follow — the property the cache relies on.

    Used by tests (and handy for debugging) to assert the prefix is stable up to
    the last ``cache_control`` marker."""
    inp = payload.get("input") or {}
    cfg_conf = (payload.get("config") or {}).get("configurable") or {}
    messages = inp.get("messages") or inp.get("input") or []
    system = next(
        (m for m in messages if isinstance(m, dict) and m.get("role") == "system"),
        None,
    )
    tools = inp.get(tools_key) or cfg_conf.get(tools_key) or []
    return json.dumps({"system": system, "tools": tools}, sort_keys=True)


# Raw finish/stop reasons a LangChain gateway may report, mapped to the
# runspec-console loop's vocabulary. Bedrock-Converse uses ``stopReason`` with
# ``max_tokens``; Anthropic uses ``stop_reason``; OpenAI-shaped chains use
# ``finish_reason`` with ``length``. Normalising the truncation spellings to
# ``"max_tokens"`` is what lets the agentic loop auto-continue a reply the
# gateway truncated at the model's output ceiling. The truncation set is shared
# with the loop's ``is_output_truncation`` so the two never drift.
_STOP_TOOL = frozenset({"tool_use", "tool_calls", "function_call"})


def extract_stop_reason(output: Any) -> str | None:
    """Pull the raw finish/stop reason from a LangServe ``output`` message, or
    ``None`` when the gateway didn't report one.

    Looks in ``response_metadata`` / ``additional_kwargs`` (where LangChain puts
    provider stop reasons) and at the top level, across the three spellings
    ``stop_reason`` (Anthropic), ``stopReason`` (Bedrock Converse), and
    ``finish_reason`` (OpenAI-shaped)."""
    msg = _unwrap(output)
    if not isinstance(msg, dict):
        return None
    sources = [
        msg.get("response_metadata"),
        msg.get("additional_kwargs"),
        msg,  # some servers surface it at the top level
    ]
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in ("stop_reason", "stopReason", "finish_reason"):
            val = src.get(key)
            if val:
                return str(val)
    return None


def normalize_stop_reason(raw: str | None) -> str:
    """Map a raw gateway stop reason to the loop's vocabulary
    (``max_tokens`` / ``tool_use`` / ``end_turn``). Unknown ⇒ ``end_turn`` (a
    safe terminal — never spins the continuation loop on a value we don't know)."""
    s = (raw or "").strip().lower()
    if s in _OUTPUT_TRUNCATION_STOPS:
        return "max_tokens"
    if s in _STOP_TOOL:
        return "tool_use"
    return "end_turn"


def _unwrap(obj: Any) -> Any:
    """Unwrap LangChain's ``dumpd`` 'constructor' envelope to the real fields.

    LangServe serialises message objects as
    ``{"lc": 1, "type": "constructor", "id": [...], "kwargs": {...}}``; the
    fields we care about (``content``, ``tool_calls``, ``usage_metadata``) live
    under ``kwargs``. A plain ``.model_dump()`` server returns them at the top
    level instead — handle both.
    """
    if isinstance(obj, dict) and obj.get("type") == "constructor" and "kwargs" in obj:
        return obj["kwargs"]
    return obj


def _content_to_text(content: Any) -> str | None:
    """Flatten message content to plain text.

    ``content`` is a string for most providers, or a list of typed blocks for
    Bedrock-Converse-style messages (``[{"type": "text", "text": ...}, ...]``).
    """
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        joined = "".join(parts)
        return joined or None
    return None


# ── /stream_events (SSE) interpretation — pure, unit-testable ─────────────────
#
# The LangServe ``astream_events`` route streams Server-Sent Events, each a JSON
# astream_events record: ``{"event": "on_chat_model_stream", "data": {"chunk":
# <AIMessageChunk>}}`` for a token, ``{"event": "on_chat_model_end", "data":
# {"output": <AIMessage>}}`` at the end. These two helpers pull the text delta and
# the final message out of one decoded event; the adapter does the SSE framing and
# builds the final ChatResponse from the end event (complete text + tool_calls +
# usage + stop reason).

_STREAM_EVENTS = frozenset({"on_chat_model_stream", "on_llm_stream"})
_END_EVENTS = frozenset({"on_chat_model_end", "on_llm_end"})


def stream_delta_text(event: Any) -> str:
    """Text delta carried by an ``on_chat_model_stream`` event, or ``""`` for any
    other event. The chunk is an ``AIMessageChunk`` (possibly in the ``dumpd``
    constructor envelope); its content is flattened the same way as a full
    message."""
    if not isinstance(event, dict) or event.get("event") not in _STREAM_EVENTS:
        return ""
    data = event.get("data")
    chunk = data.get("chunk") if isinstance(data, dict) else None
    if chunk is None:
        return ""
    msg = _unwrap(chunk)
    if isinstance(msg, dict):
        return _content_to_text(msg.get("content")) or ""
    return ""


def stream_final_output(event: Any) -> Any | None:
    """The final message ``output`` from an ``on_chat_model_end`` event (for
    :func:`parse_output` / :func:`extract_stop_reason`), or ``None`` for any other
    event."""
    if not isinstance(event, dict) or event.get("event") not in _END_EVENTS:
        return None
    data = event.get("data")
    if isinstance(data, dict):
        return data.get("output")
    return None


def parse_output(output: Any) -> tuple[str | None, list[ToolCall], dict[str, int]]:
    """Parse a LangServe ``output`` into (text, tool_calls, usage).

    Accepts a bare string, a serialised ``AIMessage`` dict, or the constructor
    envelope. LangChain normalises tool calls onto a top-level ``tool_calls``
    array of ``{"name", "args", "id"}`` regardless of provider.
    """
    if isinstance(output, str):
        return (output or None, [], {})

    msg = _unwrap(output)
    if not isinstance(msg, dict):
        return (None, [], {})

    text = _content_to_text(msg.get("content"))

    tool_calls: list[ToolCall] = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        tool_calls.append(
            ToolCall(
                id=tc.get("id") or f"call_{i}",
                name=tc.get("name", ""),
                input=tc.get("args") or tc.get("arguments") or {},
            )
        )

    usage: dict[str, int] = {}
    um = msg.get("usage_metadata") or {}
    if isinstance(um, dict):
        if um.get("input_tokens"):
            usage["input_tokens"] = int(um["input_tokens"])
        if um.get("output_tokens"):
            usage["output_tokens"] = int(um["output_tokens"])
        details = um.get("input_token_details") or {}
        if isinstance(details, dict):
            # Anthropic-family caching, as LangChain surfaces it: the cheap re-read
            # (`cache_read`) and the first-turn write (`cache_creation`). Both flow
            # to the console's context gauge / cost split via _usage_from_response.
            if details.get("cache_read"):
                usage["cache_read_input_tokens"] = int(details["cache_read"])
            if details.get("cache_creation"):
                usage["cache_creation_input_tokens"] = int(details["cache_creation"])

    return (text, tool_calls, usage)


# ── adapter ───────────────────────────────────────────────────────────────────


class LangServeAdapter(ModelAdapter):
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        system: str = DEFAULT_SYSTEM,
        api_key: str | None = None,
        base_url: str | None = None,
        api_key_command: str | None = None,
        api_key_ttl_ms: int = 0,
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        input_messages_key: str = "messages",
        input_tools_key: str = "tools",
        tools_in_config: bool = False,
        max_tokens: int = 0,
        max_tokens_key: str = "max_tokens",
        max_tokens_in: str = "kwargs",
        effort: str = "",
        effort_key: str = "reasoning_effort",
        effort_in: str = "kwargs",
        cache_markers: bool = False,
        stream_events: bool = True,
        model_limits: dict[str, Any] | None = None,
        timeout: float = 120.0,
        client: Any = None,
    ) -> None:
        if not base_url:
            raise ValueError(
                "langserve provider requires 'base_url' — the LangServe endpoint "
                "(e.g. https://gateway.corp/my-chain or .../my-chain/invoke)"
            )
        self.model = model
        self.system = system
        self.endpoint = self._endpoint(base_url)
        self.events_endpoint = self._events_endpoint(base_url)
        self._static_key = api_key
        self._auth_header = auth_header
        self._auth_scheme = auth_scheme
        self._messages_key = input_messages_key
        self._tools_key = input_tools_key
        self._tools_in_config = tools_in_config
        # Output-token ceiling forwarded to the gateway. 0 ⇒ don't send (the
        # published chain usually pins its own). Exposed so a corporate proxy in
        # front of Bedrock/Anthropic can raise it via `[llm] max_tokens`.
        self.max_tokens = int(max_tokens or 0)
        self._max_tokens_key = max_tokens_key
        self._max_tokens_in = max_tokens_in
        # Reasoning effort (`[llm] effort`). Normalised to a bare lowercased level
        # string; "" / "off" / "none" mean "send nothing" (the default). We don't
        # restrict it to the Anthropic enum here — the langserve wire shape is
        # gateway-defined, so whatever level string your chain accepts
        # ("low"/"medium"/"high"/… or a proxy-specific value) passes straight
        # through. Placement is set by effort_key / effort_in (see _build_payload).
        _eff = str(effort or "").strip().lower()
        self.effort = "" if _eff in ("", "off", "none") else _eff
        self._effort_key = effort_key
        self._effort_in = effort_in
        # Prompt-cache breakpoints (off by default): when on, the payload carries
        # ephemeral cache_control markers on system / the last tool / the last
        # message block, and the tool list is serialised deterministically so the
        # cached prefix is byte-stable across turns. Only worthwhile when the
        # gateway forwards cache_control to an Anthropic-family model — a proxy in
        # front of Bedrock/Anthropic — hence opt-in. See build_payload.
        self.cache_markers = bool(cache_markers)
        # Use the LangServe /stream_events SSE route for stream_with_tools, so a
        # long reply streams out incrementally instead of riding one long-lived
        # /invoke request. Falls back to /invoke automatically if the route is
        # unavailable; set False to force the non-streaming path.
        self.stream_events = bool(stream_events)
        # [llm.model_limits] table for capabilities() — the gateway hides the model,
        # so the operator declares its limits here (context window / output ceiling /
        # whether the proxy accepts thinking-effort). See base.capabilities_from_limits.
        self._model_limits = model_limits or {}

        self._api_key_command = api_key_command
        self._api_key_ttl_s = api_key_ttl_ms / 1000.0
        self._cached_key: str | None = None
        self._key_fetched_at: float = 0.0

        # httpx is an optional extra, so import it lazily — keeps the pure mapping
        # helpers above importable (and unit-testable) without it installed. A
        # client may be injected for tests.
        if client is not None:
            self.client = client
        else:
            import httpx

            from .. import netproxy

            self.client = httpx.AsyncClient(
                timeout=timeout, **netproxy.httpx_kwargs(base_url)
            )

    @staticmethod
    def _endpoint(base_url: str) -> str:
        url = base_url.rstrip("/")
        if url.endswith("/invoke"):
            return url
        return f"{url}/invoke"

    @staticmethod
    def _events_endpoint(base_url: str) -> str:
        """The runnable's ``/stream_events`` route — the SSE endpoint the LangServe
        ``astream_events`` API publishes alongside ``/invoke``. Derived from the
        same base (stripping a trailing ``/invoke`` if the operator pointed straight
        at it)."""
        url = base_url.rstrip("/")
        if url.endswith("/invoke"):
            url = url[: -len("/invoke")].rstrip("/")
        elif url.endswith("/stream_events"):
            return url
        return f"{url}/stream_events"

    def _token(self) -> str | None:
        """Current auth token: cached api_key_command output, else the static key."""
        if not self._api_key_command:
            return self._static_key
        now = time.monotonic()
        if (
            self._cached_key is not None
            and self._api_key_ttl_s > 0
            and (now - self._key_fetched_at) < self._api_key_ttl_s
        ):
            return self._cached_key
        key = run_api_key_command(self._api_key_command)
        self._cached_key = key
        self._key_fetched_at = now
        return key

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = self._token()
        if token:
            value = f"{self._auth_scheme} {token}" if self._auth_scheme else token
            headers[self._auth_header] = value
        return headers

    # Subclass hooks — a plugin for a non-vanilla gateway typically overrides
    # just these two to match its proprietary request/response shape, inheriting
    # the auth, rotation, and agent-loop plumbing unchanged.
    #
    # Reasoning effort, two ways:
    #   1. Your gateway takes a plain effort *string* (e.g. OpenAI-style
    #      `reasoning_effort: "high"`). That's the default — set `[llm] effort`
    #      and, if the field/slot differ, `[llm] effort_key` / `effort_in`. No
    #      code needed; build_payload places it for you.
    #   2. Your gateway wants the native Anthropic *adaptive* thinking shape
    #      (`thinking={"type":"adaptive"}` + `output_config={"effort": <level>}`,
    #      e.g. it passes the body through to Bedrock's Anthropic API). Then leave
    #      effort_key unused and inject it here in an override, e.g.:
    #
    #          from .base import apply_thinking
    #          def _build_payload(self, messages, tools):
    #              payload = super()._build_payload(messages, tools)
    #              apply_thinking(payload["kwargs"], self.effort)  # or wherever
    #              return payload                                   # your chain reads it
    #
    #      `apply_thinking` is a no-op when effort is off, so this is always safe.
    def _build_payload(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return build_payload(
            messages,
            tools,
            self.system,
            messages_key=self._messages_key,
            tools_key=self._tools_key,
            tools_in_config=self._tools_in_config,
            max_tokens=self.max_tokens,
            max_tokens_key=self._max_tokens_key,
            max_tokens_in=self._max_tokens_in,
            effort=self.effort,
            effort_key=self._effort_key,
            effort_in=self._effort_in,
            cache_markers=self.cache_markers,
        )

    def _parse_output(
        self, output: Any
    ) -> tuple[str | None, list[ToolCall], dict[str, int]]:
        return parse_output(output)

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ChatResponse:
        payload = self._build_payload(messages, tools)
        resp = await self.client.post(
            self.endpoint, json=payload, headers=self._headers()
        )
        resp.raise_for_status()
        body = resp.json()
        output = body.get("output", body) if isinstance(body, dict) else body
        text, tool_calls, usage = self._parse_output(output)
        # Surface the gateway's real stop reason so the agentic loop can auto-
        # continue a reply truncated at the output ceiling. Tool calls still win
        # (the loop runs them next); otherwise normalise what the gateway reported
        # (Bedrock/Anthropic/OpenAI spellings) — falling back to end_turn when it
        # reported nothing, exactly the previous behaviour.
        if tool_calls:
            stop_reason = "tool_use"
        else:
            stop_reason = normalize_stop_reason(extract_stop_reason(output))
        raw = SimpleNamespace(output=output, usage=SimpleNamespace(**usage))
        return ChatResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            _raw=raw,
        )

    async def stream_chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ):  # type: ignore[override]
        # No token streaming against /invoke — emit the full reply once.
        response = await self.chat(messages, tools)
        if response.text:
            yield response.text

    async def stream_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ):  # type: ignore[override]
        """Stream text deltas over the ``/stream_events`` SSE route, then emit a
        final ChatResponse with the gateway's real stop reason + usage.

        Streaming it incrementally means a long reply is not hostage to one
        long-lived ``/invoke`` request (a slow model can blow an idle-read timeout
        before the whole body arrives). Tool calls and the authoritative stop
        reason/usage are read from the terminal ``on_chat_model_end`` event, so a
        tool turn is exactly as correct as the non-streaming path.

        Falls back to the non-streaming ``/invoke`` path when ``stream_events`` is
        off, or the first time the SSE route proves unavailable (404/405/501) — and
        disables streaming for the rest of the session so it isn't re-probed."""
        if not self.stream_events:
            async for item in self._stream_via_invoke(messages, tools):
                yield item
            return
        try:
            got_end = False
            async for item in self._stream_via_events(messages, tools):
                if item[0] == "_unsupported":
                    # Route not published — stop streaming for this session and
                    # fall back; nothing was yielded before this sentinel.
                    self.stream_events = False
                    break
                if item[0] == "done":
                    got_end = True
                yield item
            if got_end:
                return
            if not self.stream_events:
                async for item in self._stream_via_invoke(messages, tools):
                    yield item
                return
            # Stream ended without a terminal event and without a fallback signal:
            # nothing more to do (any text deltas were already yielded). Emit a
            # terminal ChatResponse so the loop always sees a ("done", …).
            yield (
                "done",
                ChatResponse(text=None, tool_calls=[], stop_reason="end_turn"),
            )
        except Exception:
            # A transport error after the route was confirmed: let the agent loop's
            # network-retry / recovery handle it (re-raise) — never silently drop
            # the turn. (An unsupported-route fallback is handled above, not here.)
            raise

    async def _stream_via_invoke(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ):
        """Non-streaming fallback: one ``/invoke`` round-trip, emitted in one shot."""
        response = await self.chat(messages, tools)
        if response.text:
            yield ("text", response.text)
        yield ("done", response)

    async def _stream_via_events(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ):
        """Drive the ``/stream_events`` SSE route. Yields ``("text", delta)`` per
        token and a final ``("done", ChatResponse)`` built from the terminal event;
        yields a one-off ``("_unsupported", None)`` sentinel (before any text) when
        the route 404/405/501s, so the caller can fall back."""
        payload = self._build_payload(messages, tools)
        headers = {**self._headers(), "Accept": "text/event-stream"}
        streamed_parts: list[str] = []
        final_output: Any = None
        async with self.client.stream(
            "POST", self.events_endpoint, json=payload, headers=headers
        ) as resp:
            status = int(getattr(resp, "status_code", 200) or 200)
            if status in (404, 405, 501):
                # Drain+close so the connection is reusable, then signal fallback.
                aread = getattr(resp, "aread", None)
                if callable(aread):
                    try:
                        await aread()
                    except Exception:  # noqa: BLE001 — best-effort drain
                        pass
                yield ("_unsupported", None)
                return
            raise_for_status = getattr(resp, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
            event_name = "message"
            data_lines: list[str] = []
            async for raw in resp.aiter_lines():
                line = raw.rstrip("\r\n")
                if line == "":
                    # Blank line terminates one SSE event.
                    for item in self._dispatch_sse(event_name, data_lines):
                        if item[0] == "text":
                            streamed_parts.append(item[1])
                            yield item
                        elif item[0] == "_final":
                            final_output = item[1]
                        elif item[0] == "_end":
                            # Explicit LangServe end marker — stop reading.
                            data_lines = []
                            event_name = "message"
                            break
                    event_name = "message"
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue  # SSE comment / heartbeat
                if line.startswith("event:"):
                    event_name = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].lstrip())
            # Flush a trailing event with no terminating blank line.
            for item in self._dispatch_sse(event_name, data_lines):
                if item[0] == "text":
                    streamed_parts.append(item[1])
                    yield item
                elif item[0] == "_final":
                    final_output = item[1]
        yield ("done", self._finalize_stream(streamed_parts, final_output))

    @staticmethod
    def _dispatch_sse(event_name: str, data_lines: list[str]):
        """Interpret one accumulated SSE event. A LangServe ``event: end`` yields
        ``("_end", None)``; a data event's JSON is decoded and turned into a
        ``("text", delta)`` and/or ``("_final", output)``. Malformed JSON is
        skipped (never crashes the stream)."""
        if event_name == "end":
            return [("_end", None)]
        if not data_lines:
            return []
        raw = "\n".join(data_lines)
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            return []
        out: list[tuple[str, Any]] = []
        delta = stream_delta_text(ev)
        if delta:
            out.append(("text", delta))
        final = stream_final_output(ev)
        if final is not None:
            out.append(("_final", final))
        return out

    def _finalize_stream(
        self, streamed_parts: list[str], final_output: Any
    ) -> ChatResponse:
        """Build the terminal ChatResponse from the SSE stream: prefer the complete
        ``on_chat_model_end`` message (carries tool_calls, usage, the real stop
        reason); fall back to the concatenated text deltas when the gateway sent no
        end event. ``text`` is set (even though deltas were already streamed) so
        ``make_tool_turn`` can carry the assistant's pre-tool text."""
        if final_output is not None:
            text, tool_calls, usage = self._parse_output(final_output)
            if tool_calls:
                stop_reason = "tool_use"
            else:
                stop_reason = normalize_stop_reason(extract_stop_reason(final_output))
            raw = SimpleNamespace(output=final_output, usage=SimpleNamespace(**usage))
            return ChatResponse(
                text=text if text is not None else ("".join(streamed_parts) or None),
                tool_calls=tool_calls,
                stop_reason=stop_reason,
                _raw=raw,
            )
        text = "".join(streamed_parts) or None
        return ChatResponse(
            text=text,
            tool_calls=[],
            stop_reason="end_turn",
            _raw=SimpleNamespace(output=None, usage=SimpleNamespace()),
        )

    def make_tool_turn(
        self, response: ChatResponse, results: list[tuple[ToolCall, str]]
    ) -> list[dict[str, Any]]:
        """OpenAI-style turns — round-trip cleanly through convert_to_messages."""
        turns: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": response.text or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.input),
                        },
                    }
                    for tc, _ in results
                ],
            }
        ]
        for tc, result in results:
            turns.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        return turns

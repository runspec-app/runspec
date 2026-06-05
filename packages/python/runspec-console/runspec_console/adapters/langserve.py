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

Auth
----
Bearer token in the ``Authorization`` header, taken from a static ``api_key`` or
— for short-lived corporate tokens — an ``api_key_command`` re-run on a TTL
(same vending pattern as the Anthropic adapter). ``auth_header`` / ``auth_scheme``
override the header name and scheme.

Tool calling is structured: the returned ``AIMessage`` carries a ``tool_calls``
array which maps straight onto runspec tool calls. Streaming falls back to a
single non-streaming ``/invoke`` (mirrors the OpenAI adapter) — correct for tool
turns without the bookkeeping of reassembling a token stream.
"""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace
from typing import Any

from .base import ChatResponse, ModelAdapter, ToolCall

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


def build_payload(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    system: str,
    *,
    messages_key: str = "messages",
    tools_key: str = "tools",
    tools_in_config: bool = False,
) -> dict[str, Any]:
    """Assemble the ``/invoke`` request body.

    The system prompt is prepended as a ``system`` role message — LangChain maps
    it to a ``SystemMessage`` — so it survives even when the chain doesn't expose
    a separate system slot.
    """
    wire_messages: list[dict[str, Any]] = []
    if system.strip():
        wire_messages.append({"role": "system", "content": system})
    wire_messages.extend(messages)

    oai_tools = [_to_openai_tool(t) for t in tools] if tools else []

    payload: dict[str, Any] = {
        "input": {messages_key: wire_messages},
        "config": {},
        "kwargs": {},
    }
    if oai_tools:
        if tools_in_config:
            payload["config"] = {"configurable": {tools_key: oai_tools}}
        else:
            payload["input"][tools_key] = oai_tools
    return payload


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
        if isinstance(details, dict) and details.get("cache_read"):
            usage["cache_read_input_tokens"] = int(details["cache_read"])

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
        self._static_key = api_key
        self._auth_header = auth_header
        self._auth_scheme = auth_scheme
        self._messages_key = input_messages_key
        self._tools_key = input_tools_key
        self._tools_in_config = tools_in_config

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

            self.client = httpx.AsyncClient(timeout=timeout)

    @staticmethod
    def _endpoint(base_url: str) -> str:
        url = base_url.rstrip("/")
        if url.endswith("/invoke"):
            return url
        return f"{url}/invoke"

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
        result = subprocess.run(
            self._api_key_command,
            capture_output=True,
            text=True,
            shell=True,
        )
        key = result.stdout.strip()
        if not key:
            raise RuntimeError(
                f"api_key_command returned no output (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
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
        raw = SimpleNamespace(output=output, usage=SimpleNamespace(**usage))
        return ChatResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
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
        # Mirror the OpenAI adapter: non-streaming is simpler and correct for
        # tool turns (no partial-tool-call reassembly).
        response = await self.chat(messages, tools)
        if response.text:
            yield ("text", response.text)
        yield ("done", response)

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

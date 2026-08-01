"""
mcp_client.py — connect the console's chat agent to *external* MCP servers.

The console is already an MCP **server** producer (``runspec serve`` exposes each
runnable as a leaf MCP tool). This module is the other direction: it makes the
console an MCP **client**, so tools published by an external MCP server —
declared in ``[mcp]`` / ``[[mcp.servers]]`` in ``config.toml`` — appear in the
chat agent's toolset alongside runnables and the built-in tools, routed through
the same autonomy/confirm gate.

Design, mirroring ``observability.py`` / ``phoenix.py``:

* **Opt-in, config-gated, best-effort, never raises into the agent loop.** No
  ``[mcp]`` servers ⇒ the manager is inert. A failed connection is recorded and
  surfaced in ``status()`` (and the Settings hint); it never stops the console
  starting or a turn running.
* **The ``mcp`` SDK is a lazy import behind the ``mcp`` extra.** Importing this
  module never drags the SDK into a base install; the pure helpers
  (``tool_display_name`` / ``to_anthropic_schema`` / ``flatten_result`` /
  ``autonomy_for_server``) have no SDK dependency and are unit-tested directly.
* **Long-lived sessions.** Each enabled server holds an open ``ClientSession``
  on a dedicated background event-loop thread owned by the manager (the Bridge's
  per-turn ``asyncio.run`` loop dies between turns, so a session cannot live
  there). Tool calls are marshalled onto that loop with
  ``run_coroutine_threadsafe`` and blocked on from the worker thread the agent's
  dispatch already runs on — matching the synchronous ``_run_tool_sync``
  contract.

Three transports are supported (the operator picks per server): local ``stdio``
(spawn a subprocess), and remote ``http`` (streamable-HTTP) / ``sse``.

Safe by default: an external tool is **confirm**-gated unless the operator sets
``autonomy = "autonomous"`` on that server — the same "trust is opt-in" posture
the rest of the console takes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from .bridge_const import _MCP_PREFIX

logger = logging.getLogger(__name__)

# Anthropic tool-name grammar: ^[a-zA-Z0-9_-]{1,64}$. Display names are clamped
# to it (sanitised + length-capped); the registry maps them back to (server,
# tool) so the on-wire MCP tool name is never constrained by this.
_MAX_NAME = 64

# Default per-call / per-connect ceilings (seconds); overridable via [mcp].
DEFAULT_CALL_TIMEOUT = 60.0
DEFAULT_CONNECT_TIMEOUT = 20.0

# Cap on the text of a single tool result handed back to the model, so a chatty
# MCP tool can't blow the context window. The Bridge applies its own
# _AGENT_TOOL_OUTPUT_CAP on top; this is a module-local backstop for direct use.
_RESULT_CAP = 1_000_000


# ── pure helpers (no SDK import) ─────────────────────────────────────────────


def available() -> tuple[bool, str]:
    """``(True, "")`` when the ``mcp`` SDK imports; else ``(False, reason)``.

    Pure probe (mirrors ``observability.available`` / ``sources/schedule``);
    drives the Settings-tab "install the extra" hint. Never raises."""
    try:
        import mcp  # noqa: F401
        from mcp import ClientSession  # noqa: F401
    except Exception:  # noqa: BLE001 — any import failure ⇒ unavailable
        return (False, "MCP SDK not installed — pip install runspec-console[mcp]")
    return (True, "")


def _sanitize_component(text: str) -> str:
    """Reduce a server/tool name to the Anthropic tool-name alphabet.

    Any run of disallowed characters collapses to a single ``_``; leading/
    trailing ``_`` are trimmed. An empty result becomes ``x`` so the composed
    name is never bare."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", str(text)).strip("_")
    return cleaned or "x"


def tool_display_name(server: str, tool: str) -> str:
    """Namespaced, grammar-valid display name for an external tool:
    ``mcp_<server>_<tool>``, sanitised and capped at 64 chars.

    Collisions after sanitisation/truncation are resolved by the manager (it
    owns the registry); this is the deterministic base name."""
    base = f"{_MCP_PREFIX}{_sanitize_component(server)}_{_sanitize_component(tool)}"
    if len(base) > _MAX_NAME:
        base = base[:_MAX_NAME].rstrip("_") or base[:_MAX_NAME]
    return base


def to_anthropic_schema(
    name: str, description: str, input_schema: Any
) -> dict[str, Any]:
    """Convert an MCP tool's ``(name, description, input_schema)`` to the
    ``{name, description, input_schema}`` Anthropic tool shape.

    An MCP ``input_schema`` is already JSON Schema; we only guarantee the
    top-level ``type: object`` Anthropic requires (defaulting an empty/absent
    schema to ``{"type": "object", "properties": {}}``)."""
    if isinstance(input_schema, dict) and input_schema:
        schema = dict(input_schema)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
    else:
        schema = {"type": "object", "properties": {}}
    return {
        "name": name,
        "description": str(description or "").strip() or f"External MCP tool {name}.",
        "input_schema": schema,
    }


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from either an object attribute or a dict item — so the
    flattener/converters work on live SDK models *and* plain dicts (tests)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def flatten_result(result: Any, *, cap: int = _RESULT_CAP) -> str:
    """Flatten an MCP ``CallToolResult`` (SDK model or dict) to a string.

    Text content blocks are concatenated; non-text blocks (image / resource) are
    noted by type. ``structured_content`` (when present) is appended as JSON —
    the machine-readable payload the model often wants. An error result is
    prefixed ``Error:`` so the agent reads it as a failure it can act on."""
    is_error = bool(_attr(result, "is_error", _attr(result, "isError", False)))
    content = _attr(result, "content", None) or []
    parts: list[str] = []
    for block in content:
        btype = str(_attr(block, "type", "") or "")
        if btype == "text" or _attr(block, "text") is not None:
            parts.append(str(_attr(block, "text", "") or ""))
        elif btype:
            parts.append(f"[{btype} content omitted]")
    structured = _attr(result, "structured_content", _attr(result, "structuredContent"))
    if structured not in (None, {}, []):
        try:
            parts.append(json.dumps(structured, default=str))
        except Exception:  # noqa: BLE001 — a non-serialisable payload is non-fatal
            parts.append(str(structured))
    text = "\n".join(p for p in parts if p) or (
        "(tool returned no content)" if not is_error else "(tool reported an error)"
    )
    if len(text) > cap:
        text = text[:cap] + "\n…[truncated]"
    return f"Error: {text}" if is_error else text


def autonomy_for_server(server_cfg: dict[str, Any]) -> str:
    """Autonomy level for a server's tools: the operator's explicit
    ``autonomy`` override when set, otherwise ``confirm`` (safe by default —
    an external tool is a side effect until the operator says it isn't)."""
    override = str((server_cfg or {}).get("autonomy") or "").strip().lower()
    if override in ("autonomous", "confirm", "manual"):
        return override
    return "confirm"


def normalize_proxy(proxy: Any) -> str:
    """A per-server HTTP CONNECT proxy URL, scheme-normalised (``host:port`` →
    ``http://host:port``); ``""`` when unset. Mirrors ``[ssh] proxy`` / Config
    Sync's ``proxy`` so a server on a different corporate network gets its own
    egress."""
    p = str(proxy or "").strip()
    if not p:
        return ""
    return p if "://" in p else f"http://{p}"


def apply_credential_header(
    headers: dict[str, str], cfg: dict[str, Any], cred: dict[str, Any] | None
) -> None:
    """Add an auth header for an http/sse server from its resolved credential,
    in place. A header the operator set explicitly always wins (never
    overwritten). With ``credential_header`` set, the secret is sent raw under
    that name (e.g. ``X-API-Key``); otherwise a ``userpass`` credential becomes
    HTTP ``Basic`` and a ``token``/``password`` credential ``Bearer``."""
    if not cred or not str(cred.get("secret") or ""):
        return
    target = str(cfg.get("credential_header") or "").strip() or "Authorization"
    if any(k.lower() == target.lower() for k in headers):
        return  # explicit header wins
    secret = str(cred["secret"])
    if str(cfg.get("credential_header") or "").strip():
        headers[target] = secret  # custom header carries the raw secret
    elif str(cred.get("kind") or "") == "userpass":
        import base64

        raw = f"{cred.get('username', '') or ''}:{secret}".encode()
        headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
    else:
        headers["Authorization"] = f"Bearer {secret}"


def _proxy_http_factory(proxy_url: str) -> Any:
    """An ``httpx_client_factory`` (also directly callable) that builds an httpx
    AsyncClient routed through ``proxy_url``, with MCP's SSE-friendly defaults
    (follow-redirects + long read timeout). Used only when a server sets a
    ``proxy`` — the default (proxy-less) path keeps mcp's own client factory."""
    import httpx

    try:
        from mcp.shared._httpx_utils import (
            MCP_DEFAULT_SSE_READ_TIMEOUT,
            MCP_DEFAULT_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 — fall back to MCP's documented defaults
        MCP_DEFAULT_TIMEOUT, MCP_DEFAULT_SSE_READ_TIMEOUT = 30.0, 300.0

    def factory(headers: Any = None, timeout: Any = None, auth: Any = None) -> Any:
        kwargs: dict[str, Any] = {"follow_redirects": True, "proxy": proxy_url}
        kwargs["timeout"] = (
            timeout
            if timeout is not None
            else httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)
        )
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return factory


# ── live session state + manager ─────────────────────────────────────────────


class _ServerState:
    """Per-server bookkeeping shared between the loop thread and callers."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.name = str(cfg.get("name") or "")
        self.transport = str(cfg.get("transport") or "stdio")
        self.session: Any = None
        self.tools: list[Any] = []  # live SDK Tool models
        # bare MCP tool name → display name, filled at connect time.
        self.display_names: dict[str, str] = {}
        self.connected = False
        self.error = ""
        # threading.Event (waited on by start() from the caller thread) —
        # set once the server has connected *or* failed.
        self.ready = threading.Event()
        # asyncio.Event created on the loop by the supervisor; signals shutdown.
        self.stop_event: Any = None


class McpManager:
    """Owns the background event loop and the live MCP client sessions.

    Constructed cheaply (no SDK import, no connection) so the Bridge can hold one
    unconditionally; ``start()`` (called at app bring-up) connects the configured
    servers. Everything is best-effort and guarded — a broken server never raises
    into the caller."""

    def __init__(
        self, config_reader: Any = None, credential_resolver: Any = None
    ) -> None:
        # Injectable for tests; defaults to config.read_mcp (imported lazily so
        # importing this module has no config side effects).
        self._config_reader = config_reader
        # credential_resolver(cred_id) -> {"kind","username","secret","env":{…}}
        # or None. Injected by the Bridge (keychain-backed); kept out of this
        # module so it stays SDK/keyring-free and unit-testable with a fake.
        self._credential_resolver = credential_resolver
        self._lock = threading.Lock()
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._servers: dict[str, _ServerState] = {}
        # display name → (server name, bare tool name)
        self._registry: dict[str, tuple[str, str]] = {}
        self._started = False
        self._unavailable_reason = ""
        self._call_timeout = DEFAULT_CALL_TIMEOUT
        self._connect_timeout = DEFAULT_CONNECT_TIMEOUT

    # -- config --------------------------------------------------------------

    def _read_config(self) -> dict[str, Any]:
        if self._config_reader is not None:
            return self._config_reader()
        from . import config

        return config.read_mcp()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Connect every enabled configured server (idempotent, best-effort).

        Blocks up to ``connect_timeout`` per server so the first turn after
        launch sees the tools; a server that is slow or down just lands
        disconnected and shows in ``status()``."""
        with self._lock:
            if self._started:
                return
            self._started = True
            try:
                cfg = self._read_config()
            except Exception:  # noqa: BLE001
                logger.warning("reading [mcp] config failed", exc_info=True)
                return
            if not cfg.get("enabled", True):
                return
            servers = [s for s in cfg.get("servers", []) if s.get("enabled", True)]
            if not servers:
                return
            avail, reason = available()
            if not avail:
                self._unavailable_reason = reason
                logger.info("MCP servers configured but %s", reason)
                return
            self._call_timeout = float(cfg.get("call_timeout") or DEFAULT_CALL_TIMEOUT)
            self._connect_timeout = float(
                cfg.get("connect_timeout") or DEFAULT_CONNECT_TIMEOUT
            )
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_loop, name="mcp-loop", daemon=True
            )
            self._thread.start()
            states = [_ServerState(s) for s in servers]
            for st in states:
                self._servers[st.name] = st
            loop = self._loop
        # Launch supervisors and wait (outside the lock) for each to settle.
        for st in states:
            asyncio.run_coroutine_threadsafe(self._serve_one(st), loop)
        for st in states:
            if not st.ready.wait(timeout=self._connect_timeout):
                st.error = st.error or "connect timed out"
                logger.warning("MCP server %s did not connect in time", st.name)
        logger.info(
            "MCP manager started: %d/%d server(s) connected, %d tool(s)",
            sum(1 for s in states if s.connected),
            len(states),
            len(self._registry),
        )

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            with contextlib.suppress(Exception):
                self._loop.close()

    async def _serve_one(self, st: _ServerState) -> None:
        """Open + hold one server's session until shutdown. Runs on the loop
        thread. Any failure is recorded on the state and the ready flag set so
        ``start()`` never blocks the full timeout on a fast failure."""
        st.stop_event = asyncio.Event()
        try:
            cred = self._resolve_credential(st.cfg)
            async with contextlib.AsyncExitStack() as stack:
                read, write = await self._open_streams(stack, st.cfg, cred)
                from mcp import ClientSession

                session = await stack.enter_async_context(
                    ClientSession(read, write, read_timeout_seconds=None)
                )
                await session.initialize()
                listed = await session.list_tools()
                st.session = session
                st.tools = list(getattr(listed, "tools", []) or [])
                st.connected = True
                self._register(st)
                logger.info(
                    "MCP server %s connected (%s): %d tool(s)",
                    st.name,
                    st.transport,
                    len(st.tools),
                )
                st.ready.set()
                await st.stop_event.wait()
        except Exception as exc:  # noqa: BLE001 — record + surface, never propagate
            st.error = str(exc) or exc.__class__.__name__
            st.connected = False
            logger.warning("MCP server %s failed: %s", st.name, st.error)
        finally:
            st.session = None
            st.connected = False
            st.ready.set()

    def _resolve_credential(self, cfg: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve this server's ``credential`` id to its secret material via the
        injected resolver (keychain-backed), or ``None``. Best-effort — a missing
        resolver / id / secret just means no credential is applied."""
        cid = str(cfg.get("credential") or "").strip()
        if not cid or self._credential_resolver is None:
            return None
        try:
            return self._credential_resolver(cid)
        except Exception:  # noqa: BLE001 — never let a credential lookup break connect
            logger.warning(
                "resolving MCP credential %r for server %r failed",
                cid,
                cfg.get("name"),
                exc_info=True,
            )
            return None

    async def _open_streams(
        self, stack: Any, cfg: dict[str, Any], cred: dict[str, Any] | None
    ) -> tuple[Any, Any]:
        """Enter the transport for ``cfg`` on the exit stack and return its
        ``(read, write)`` streams. All three mcp transports yield that pair.

        ``cred`` (when set) supplies auth: its name-derived env vars for a stdio
        server, or an Authorization/custom header for http/sse. ``cfg['proxy']``
        routes an http/sse server through an HTTP CONNECT proxy (and is exported
        to a stdio subprocess as the standard ``*_PROXY`` vars)."""
        transport = str(cfg.get("transport") or "stdio").strip().lower()
        proxy = normalize_proxy(cfg.get("proxy"))
        if transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            user_env = {str(k): str(v) for k, v in (cfg.get("env") or {}).items()}
            cred_env = dict(cred.get("env") or {}) if cred else {}
            proxy_env = (
                {"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "ALL_PROXY": proxy}
                if proxy
                else {}
            )
            # Merge over the current environment so the child inherits PATH etc.
            # (a bare env dict would strip the subprocess of everything else).
            env = {**os.environ, **user_env, **cred_env, **proxy_env}
            params = StdioServerParameters(
                command=str(cfg.get("command") or ""),
                args=[str(a) for a in (cfg.get("args") or [])],
                env=env or None,
                cwd=str(cfg.get("cwd") or "") or None,
            )
            return await stack.enter_async_context(stdio_client(params))
        headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        apply_credential_header(headers, cfg, cred)
        url = str(cfg.get("url") or "")
        if transport == "sse":
            from mcp.client.sse import sse_client

            if proxy:
                return await stack.enter_async_context(
                    sse_client(
                        url,
                        headers=headers or None,
                        httpx_client_factory=_proxy_http_factory(proxy),
                    )
                )
            return await stack.enter_async_context(
                sse_client(url, headers=headers or None)
            )
        # http / streamable-http (default for a URL server)
        from mcp.client.streamable_http import (
            create_mcp_http_client,
            streamable_http_client,
        )

        http_client = None
        if proxy:
            http_client = await stack.enter_async_context(
                _proxy_http_factory(proxy)(headers=headers or None)
            )
        elif headers:
            http_client = await stack.enter_async_context(
                create_mcp_http_client(headers=headers)
            )
        return await stack.enter_async_context(
            streamable_http_client(url, http_client=http_client)
        )

    def _register(self, st: _ServerState) -> None:
        """Assign each of a connected server's tools a unique display name and
        record the reverse map. Runs on the loop thread; guarded by the lock."""
        with self._lock:
            for tool in st.tools:
                bare = str(_attr(tool, "name", "") or "")
                if not bare:
                    continue
                base = tool_display_name(st.name, bare)
                name = base
                i = 2
                while name in self._registry and self._registry[name] != (
                    st.name,
                    bare,
                ):
                    suffix = f"_{i}"
                    name = base[: _MAX_NAME - len(suffix)].rstrip("_") + suffix
                    i += 1
                self._registry[name] = (st.name, bare)
                st.display_names[bare] = name

    def stop(self) -> None:
        """Signal every supervisor to unwind its session (terminating any stdio
        subprocess) and stop the loop. Best-effort; safe to call more than once
        and safe when never started."""
        with self._lock:
            loop = self._loop
            states = list(self._servers.values())
            self._servers.clear()
            self._registry.clear()
            self._loop = None
            self._thread, thread = None, self._thread
            self._started = False
        if loop is None:
            return

        def _signal() -> None:
            for st in states:
                if st.stop_event is not None:
                    st.stop_event.set()

        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(_signal)
        # Give supervisors a moment to unwind cleanly, then stop the loop.
        if thread is not None:
            thread.join(timeout=5.0)
        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(loop.stop)

    def reload(self) -> None:
        """Reconnect from the current config (stop, then start)."""
        self.stop()
        self.start()

    # -- query / call --------------------------------------------------------

    def is_mcp_tool(self, name: str) -> bool:
        with self._lock:
            return name in self._registry

    def autonomy_for(self, name: str) -> str:
        """Autonomy for a display name — its server's override, else confirm."""
        with self._lock:
            entry = self._registry.get(name)
            st = self._servers.get(entry[0]) if entry else None
        if st is None:
            return "confirm"
        return autonomy_for_server(st.cfg)

    def tools(self) -> list[dict[str, Any]]:
        """Anthropic-format schemas for every connected server's tools, named by
        their display names. Empty when nothing is connected."""
        out: list[dict[str, Any]] = []
        with self._lock:
            states = list(self._servers.values())
        for st in states:
            if not st.connected:
                continue
            for tool in st.tools:
                bare = str(_attr(tool, "name", "") or "")
                display = st.display_names.get(bare)
                if not display:
                    continue
                desc = _attr(tool, "description", "") or ""
                server_note = f" (via MCP server '{st.name}')"
                out.append(
                    to_anthropic_schema(
                        display,
                        f"{str(desc).strip()}{server_note}".strip(),
                        _attr(tool, "input_schema", _attr(tool, "inputSchema")),
                    )
                )
        return out

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None, timeout: float | None = None
    ) -> str:
        """Call an external tool by display name and return its flattened text.

        Marshals the coroutine onto the manager's loop and blocks the caller
        (the agent-dispatch worker thread). Returns an ``Error: …`` string on
        any failure — never raises — so the agent turn survives a bad tool."""
        with self._lock:
            entry = self._registry.get(name)
            st = self._servers.get(entry[0]) if entry else None
            loop = self._loop
        if entry is None or st is None or loop is None or not st.connected:
            return f"Error: MCP tool '{name}' is not connected."
        _server, bare = entry
        session = st.session
        if session is None:
            return f"Error: MCP tool '{name}' has no live session."
        deadline = float(timeout or self._call_timeout)
        try:
            fut = asyncio.run_coroutine_threadsafe(
                session.call_tool(bare, arguments or {}, read_timeout_seconds=deadline),
                loop,
            )
        except Exception as exc:  # noqa: BLE001 — loop gone / not running
            return f"Error: MCP tool '{name}' could not be dispatched: {exc}"
        try:
            result = fut.result(timeout=deadline + 5.0)
        except FutureTimeout:
            fut.cancel()
            return f"Error: MCP tool '{name}' timed out after {deadline:.0f}s."
        except Exception as exc:  # noqa: BLE001 — tool raised
            return f"Error: MCP tool '{name}' failed: {exc}"
        return flatten_result(result)

    def status(self) -> dict[str, Any]:
        """Report the manager's state for the Settings tab / console context:
        overall availability plus a per-server summary."""
        avail, reason = available()
        with self._lock:
            states = list(self._servers.values())
            registry_size = len(self._registry)
        servers = [
            {
                "name": st.name,
                "transport": st.transport,
                "connected": st.connected,
                "tool_count": len(st.display_names),
                "error": st.error,
                # Non-secret shaping fields, surfaced for a future Settings tab
                # (e.g. to pre-select the credential in a dropdown). Never the
                # secret — only the credential's id.
                "credential": str(st.cfg.get("credential") or ""),
                "proxy": normalize_proxy(st.cfg.get("proxy")),
            }
            for st in states
        ]
        return {
            "available": avail,
            "reason": self._unavailable_reason or ("" if avail else reason),
            "started": self._started,
            "tool_count": registry_size,
            "servers": servers,
        }

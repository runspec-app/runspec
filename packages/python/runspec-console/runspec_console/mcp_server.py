"""
mcp_server.py — the console's own in-process MCP **server** (console-as-MCP-server).

The mirror of ``mcp_client.py``: instead of the console *connecting to* outside MCP
servers, this hosts a streamable-HTTP MCP server on ``127.0.0.1`` that an external
coding agent (Claude Code, Cursor, …) connects to, publishing the console's fleet
runnables as **serve-identical leaf tools** (subcommands flattened to
``parent_child`` tools, ``password`` args omitted, each runnable's declared
``autonomy`` carried).

A ``confirm``/``supervised`` call **parks in the console's existing confirm-card
UI** and blocks up to a bounded window (the confirm timeout, kept under the
client's own per-tool-call ceiling); approved in time → the runnable runs and its
output is returned; not approved in time → a graceful, *resumable* "awaiting
approval" result is returned (the confirm stays parked, the agent can retry).
``autonomous`` runs immediately; ``manual`` is refused. Both agent- and
human-initiated runs land in the same History (the run is tagged ``trigger=mcp``).

Design mirrors ``mcp_client.McpManager``: this object is constructed cheaply (no SDK
import, no socket) so the Bridge can hold one unconditionally; ``start()`` (called
at app bring-up when ``[mcp_server] enabled``) imports the SDK (v2, behind the
``mcp`` extra) + uvicorn (transitive) and serves on a background event-loop thread.
Everything is best-effort and guarded — a failure never breaks console startup.

The gate + execution themselves live in the Bridge (which owns ``_register_confirm``
/ ``invoke_runnable``); this module is injected a single blocking ``execute``
callback and stays SDK-only, keeping the transport/listing/heartbeat here and the
autonomy/run wiring in ``bridge_mcp_server``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("runspec-console.mcp_server")

# Most-restrictive-wins ordering for autonomy escalation (mirrors the core rule).
_AUTONOMY_RANK = {"autonomous": 0, "supervised": 1, "confirm": 2, "manual": 3}
_MAX_TOOL_NAME = 128


def available() -> tuple[bool, str]:
    """Whether the MCP server SDK + ASGI server can be imported (the ``mcp`` extra).

    Returns ``(ok, reason)``; ``reason`` is empty when ok. Lazy so importing this
    module never requires the extra."""
    try:
        import mcp.server.lowlevel  # noqa: F401
        import uvicorn  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"the 'mcp' extra is not installed ({exc})"
    return True, ""


def escalate_autonomy(levels: list[str]) -> str:
    """The most-restrictive of a list of autonomy levels (unknown ⇒ ``confirm``,
    fail-closed). Empty ⇒ ``confirm``."""
    best = "autonomous"
    saw = False
    for lvl in levels:
        lvl = str(lvl or "").strip().lower()
        if not lvl:
            continue
        saw = True
        if _AUTONOMY_RANK.get(lvl, _AUTONOMY_RANK["confirm"]) > _AUTONOMY_RANK.get(
            best, 0
        ):
            best = lvl if lvl in _AUTONOMY_RANK else "confirm"
    return best if saw else "confirm"


def sanitize_tool_name(name: str) -> str:
    """An MCP-safe tool name: keep ``[A-Za-z0-9_-]``, replace the rest with ``_``."""
    out = "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(name))
    return (out[:_MAX_TOOL_NAME] or "tool").strip("_") or "tool"


def _arg_property(arg: dict[str, Any]) -> dict[str, Any]:
    """One discovered arg → a JSON-Schema property. Mirrors
    ``catalog.arg_to_property`` but kept local so this module has no import cycle."""
    type_map = {
        "int": "integer",
        "float": "number",
        "flag": "boolean",
        "bool": "boolean",
    }
    prop: dict[str, Any] = {"type": type_map.get(str(arg.get("type")), "string")}
    if arg.get("description"):
        prop["description"] = str(arg["description"])
    if arg.get("default") is not None:
        prop["default"] = arg["default"]
    if arg.get("options"):
        prop["enum"] = list(arg["options"])
    rng = arg.get("range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        prop["minimum"], prop["maximum"] = rng[0], rng[1]
    return prop


def _input_schema(args: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a tool ``inputSchema`` from a node's effective args, omitting
    ``password`` args (secrets never reach an agent-facing schema)."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for arg in args or []:
        if str(arg.get("type")) == "password":
            continue
        name = str(arg.get("name") or "")
        if not name:
            continue
        properties[name] = _arg_property(arg)
        if arg.get("required"):
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


@dataclass
class ExecTool:
    """One resolved leaf tool: how it's presented + how to run it."""

    name: str  # the MCP tool name (deduped, sanitized)
    description: str
    input_schema: dict[str, Any]
    host: str
    group: str
    runnable: str
    command_path: list[str]
    autonomy: str

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def _flatten(node: dict[str, Any], prefix: tuple[str, ...]) -> list[ExecTool]:
    """Flatten one discovered runnable node (and its ``commands`` subtree) into leaf
    ExecTools with serve-identical underscore-joined names."""
    name = str(node.get("name") or "")
    if not name:
        return []
    parts = prefix + (name,)
    commands = node.get("commands")
    args = node.get("args") or []
    autonomy = escalate_autonomy(
        [str(node.get("autonomy") or "")] + [a.get("autonomy") for a in args]
    )

    def make(node_parts: tuple[str, ...]) -> ExecTool:
        return ExecTool(
            name=sanitize_tool_name("_".join(node_parts)),
            description=str(node.get("description") or ""),
            input_schema=_input_schema(args),
            host=str(node.get("host") or ""),
            group=str(node.get("group") or ""),
            runnable=node_parts[0],
            command_path=list(node_parts[1:]),
            autonomy=autonomy,
        )

    tools: list[ExecTool] = []
    if isinstance(commands, dict) and commands:
        # A require-command parent is not itself callable — only its leaves are.
        if not node.get("requireCommand"):
            tools.append(make(parts))
        for sub in commands.values():
            if isinstance(sub, dict):
                tools.extend(_flatten(sub, parts))
    else:
        tools.append(make(parts))
    return tools


def build_tools(
    runnables: list[dict[str, Any]], *, default_autonomy: str | None = None
) -> tuple[list[ExecTool], dict[str, ExecTool]]:
    """Flatten discovered runnables into leaf ExecTools + a name→ExecTool registry.

    Names are the serve-identical ``runnable[_sub…]``; the **same leaf discovered on
    several hosts** is disambiguated by prefixing the sanitized ``<host>__`` (then a
    numeric suffix) so every host stays reachable. ``default_autonomy`` (when set)
    is a floor applied most-restrictively to every tool."""
    registry: dict[str, ExecTool] = {}
    ordered: list[ExecTool] = []
    for runnable in runnables or []:
        for tool in _flatten(runnable, ()):
            if default_autonomy:
                tool.autonomy = escalate_autonomy([tool.autonomy, default_autonomy])
            name = tool.name
            existing = registry.get(name)
            if existing is not None and (
                existing.host,
                existing.command_path,
                existing.runnable,
            ) != (tool.host, tool.command_path, tool.runnable):
                # collision across hosts → host-prefix, then numeric
                base = sanitize_tool_name(f"{tool.host}__{tool.name}")
                name = base
                i = 2
                while name in registry:
                    name = sanitize_tool_name(f"{base}_{i}")
                    i += 1
                tool = ExecTool(**{**tool.__dict__, "name": name})
            elif existing is not None:
                continue  # identical tool already registered (same host+cmd)
            registry[name] = tool
            ordered.append(tool)
    return ordered, registry


# ── the server host ─────────────────────────────────────────────────────────────

# How often the parked/running handler emits a keep-alive progress notification
# while it waits (well under any client read window).
_HEARTBEAT_S = 15.0


@dataclass
class _State:
    started: bool = False
    running: bool = False
    error: str = ""
    host: str = "127.0.0.1"
    port: int = 0
    token: str | None = None
    registry: dict[str, ExecTool] = field(default_factory=dict)


class ConsoleMcpServer:
    """Hosts the console's fleet as a streamable-HTTP MCP server on a background
    loop thread. Constructed cheaply; ``start()`` does the SDK import + serve.

    Injected callbacks (all provided by the Bridge, keeping this module SDK-only):
      * ``config_reader() -> dict`` — ``config.read_mcp_server()``.
      * ``tools_provider() -> list[dict]`` — the visibility-filtered discovered
        runnables to publish (called on every ``tools/list``, so discovery stays
        live).
      * ``execute(tool: ExecTool, arguments: dict) -> tuple[str, bool]`` — run the
        tool through the autonomy gate + executor, returning ``(text, is_error)``.
        Blocking; the server calls it off the loop via a worker thread and
        heartbeats progress until it returns.
      * ``brand_name() -> str`` *(optional)* — the ``[console.theme] brand_name``,
        so the protocol-level server name a connecting client sees is the
        white-label brand rather than the literal ``runspec-console``.
    """

    def __init__(
        self,
        *,
        config_reader: Callable[[], dict[str, Any]],
        tools_provider: Callable[[], list[dict[str, Any]]],
        execute: Callable[[ExecTool, dict[str, Any]], tuple[str, bool]],
        brand_name: Callable[[], str] | None = None,
    ) -> None:
        self._config_reader = config_reader
        self._tools_provider = tools_provider
        self._execute = execute
        self._brand_name = brand_name
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._uvicorn: Any = None
        self._state = _State()
        self._default_autonomy: str | None = None

    # -- introspection -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            s = self._state
            return {
                "running": s.running,
                "error": s.error,
                "host": s.host,
                "port": s.port,
                "url": (f"http://{s.host}:{s.port}/mcp" if s.running else ""),
                "auth": bool(s.token),
                "tools": len(s.registry),
            }

    def _build_registry(self, default_autonomy: str | None) -> dict[str, ExecTool]:
        try:
            runnables = list(self._tools_provider() or [])
        except Exception:  # noqa: BLE001 — a discovery hiccup shouldn't 500 the list
            logger.warning("mcp_server tools_provider failed", exc_info=True)
            runnables = []
        _, registry = build_tools(runnables, default_autonomy=default_autonomy)
        return registry

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Serve if ``[mcp_server] enabled`` and the SDK is available. Idempotent,
        best-effort; records failures in ``status()`` rather than raising."""
        with self._lock:
            if self._state.started:
                return
            self._state.started = True
            try:
                cfg = self._config_reader()
            except Exception:  # noqa: BLE001
                logger.warning("reading [mcp_server] config failed", exc_info=True)
                return
            if not cfg.get("enabled"):
                return
            ok, reason = available()
            if not ok:
                self._state.error = reason
                logger.info("[mcp_server] enabled but %s", reason)
                return
            self._state.host = str(cfg.get("host") or "127.0.0.1")
            self._state.port = int(cfg.get("port") or 8765)
            self._state.token = cfg.get("token") or None
            self._default_autonomy = cfg.get("default_autonomy") or None
        self._thread = threading.Thread(
            target=self._run, name="mcp-server", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        uv = self._uvicorn
        if uv is not None:
            uv.should_exit = True
        with self._lock:
            self._state.running = False

    def restart(self) -> None:
        """Stop the current server (if any) and start fresh from current config —
        used when the Settings tab saves ``[mcp_server]``. Best-effort; a rebind
        that races the old socket surfaces as an error in ``status()``."""
        self.stop()
        t = self._thread
        if t is not None:
            t.join(timeout=4)
        with self._lock:
            self._state = _State()
        self._uvicorn = None
        self._thread = None
        self.start()

    def _run(self) -> None:
        import asyncio

        try:
            import uvicorn

            server = self._build_server()
            app = server.streamable_http_app(
                streamable_http_path="/mcp", host=self._state.host
            )
            if self._state.token:
                app = _BearerAuthMiddleware(app, self._state.token)
            config = uvicorn.Config(
                app,
                host=self._state.host,
                port=self._state.port,
                log_level="warning",
                lifespan="on",
            )
            self._uvicorn = uvicorn.Server(config)
            with self._lock:
                self._state.running = True
                self._state.error = ""
            logger.info(
                "console MCP server on http://%s:%d/mcp (auth=%s)",
                self._state.host,
                self._state.port,
                bool(self._state.token),
            )
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._uvicorn.serve())
        except Exception as exc:  # noqa: BLE001 — record + surface, never propagate
            with self._lock:
                self._state.running = False
                self._state.error = str(exc) or exc.__class__.__name__
            logger.warning("console MCP server stopped: %s", exc, exc_info=True)

    def _build_server(self) -> Any:
        import mcp.types as types
        from mcp.server.lowlevel import Server

        async def on_list_tools(ctx: Any, params: Any) -> Any:
            registry = self._build_registry(self._default_autonomy)
            with self._lock:
                self._state.registry = registry
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name=t.name,
                        description=t.description or None,
                        input_schema=t.input_schema,
                    )
                    for t in registry.values()
                ]
            )

        async def on_call_tool(ctx: Any, params: Any) -> Any:
            import asyncio

            with self._lock:
                tool = self._state.registry.get(params.name)
            if tool is None:
                # rebuild once — the client may be calling before a fresh list
                tool = self._build_registry(self._default_autonomy).get(params.name)
            if tool is None:
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text", text=f"unknown tool: {params.name}"
                        )
                    ],
                    is_error=True,
                )
            args = dict(params.arguments or {})
            worker = asyncio.create_task(asyncio.to_thread(self._execute, tool, args))
            step = 0
            while True:
                done, _ = await asyncio.wait({worker}, timeout=_HEARTBEAT_S)
                if worker in done:
                    break
                step += 1
                try:
                    await ctx.session.report_progress(
                        progress=float(step),
                        total=None,
                        message="awaiting operator approval in the console…",
                    )
                except Exception:  # noqa: BLE001 — progress is best-effort
                    pass
            text, is_error = worker.result()
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=text)],
                is_error=bool(is_error),
            )

        brand = self._resolve_brand_name()
        return Server(
            brand,
            version="1",
            instructions=(
                f"{brand} fleet runnables. confirm-level tools park for "
                "human approval in the console UI."
            ),
            on_list_tools=on_list_tools,
            on_call_tool=on_call_tool,
        )

    def _resolve_brand_name(self) -> str:
        """The white-label brand for the protocol-level server name (falls back to
        ``runspec-console``). Best-effort — a failing/empty resolver never breaks
        bring-up."""
        if self._brand_name is None:
            return "runspec-console"
        try:
            name = str(self._brand_name() or "").strip()
        except Exception:  # noqa: BLE001 — brand is cosmetic, never fatal
            logger.debug("mcp_server brand_name lookup failed", exc_info=True)
            return "runspec-console"
        return name or "runspec-console"


class _BearerAuthMiddleware:
    """Minimal ASGI middleware requiring ``Authorization: Bearer <token>`` on every
    HTTP request. Localhost is the primary bound; this guards other local processes."""

    def __init__(self, app: Any, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = {
                k.decode().lower(): v.decode() for k, v in scope.get("headers", [])
            }
            if headers.get("authorization") != self._expected:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"www-authenticate", b"Bearer")],
                    }
                )
                await send(
                    {"type": "http.response.body", "body": b'{"error":"unauthorized"}'}
                )
                return
        await self._app(scope, receive, send)

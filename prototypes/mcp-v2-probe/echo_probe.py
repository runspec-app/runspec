"""End-to-end probe for MCP Python SDK v2.2.0.

Builds a streamable-HTTP MCP server that exposes ONE dynamic tool `echo`
(explicit JSON input schema, not derived from a function signature), whose
async handler emits progress notifications while it runs. The server runs on a
background asyncio-loop thread inside this process; the client then connects
over streamable-HTTP and calls `echo`, printing the result and the progress
notifications it received.

Optional bearer-token guard: set REQUIRE_TOKEN=1 to wrap the ASGI app in a
Starlette middleware that requires `Authorization: Bearer <SECRET>`.

Run:  ./.venv/bin/python /home/user/runspec/prototypes/mcp-v2-probe/echo_probe.py
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import mcp_types as types
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from starlette.types import ASGIApp, Receive, Scope, Send

HOST = "127.0.0.1"
PORT = 8931
PATH = "/mcp"
URL = f"http://{HOST}:{PORT}{PATH}"
SECRET = "s3cr3t-token"
REQUIRE_TOKEN = os.environ.get("REQUIRE_TOKEN") == "1"


# ---------------------------------------------------------------------------
# 1. Dynamic tool catalog (names + JSON schemas computed at runtime)
# ---------------------------------------------------------------------------
def build_catalog() -> list[types.Tool]:
    """The tool list is data, not decorated Python functions."""
    return [
        types.Tool(
            name="echo",
            title="Echo",
            description="Echo the given text back after some fake work.",
            # NOTE: field is `input_schema` (wire alias `inputSchema`). An
            # explicit JSON Schema dict — NOT inferred from a signature.
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )
    ]


# ---------------------------------------------------------------------------
# 2. Low-level request handlers (constructor callbacks, not decorators in v2)
# ---------------------------------------------------------------------------
async def on_list_tools(
    ctx: ServerRequestContext,
    params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=build_catalog())


async def on_call_tool(
    ctx: ServerRequestContext,
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    if params.name != "echo":
        # Report a tool error as a result (not a raised exception)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"unknown tool: {params.name}")],
            is_error=True,
        )

    args = params.arguments or {}
    text = args.get("text", "")

    # 3. Progress notifications from inside the handler.
    #    `ctx.session.report_progress` uses this request's progress token
    #    (from the incoming _meta.progressToken) automatically, and is a
    #    no-op if the client did not request progress.
    total = 3.0
    for step in range(1, 4):
        await ctx.session.report_progress(
            progress=float(step),
            total=total,
            message=f"working step {step}/3",
        )
        await asyncio.sleep(0.05)

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"echo: {text}")],
    )


def build_server() -> Server:
    return Server(
        "echo-probe",
        version="0.1.0",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


# ---------------------------------------------------------------------------
# 6. Bearer-token guard as a plain ASGI middleware wrapping the app.
# ---------------------------------------------------------------------------
class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            auth = headers.get("authorization", "")
            if auth != f"Bearer {self.token}":
                body = b'{"error":"unauthorized"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# 4. Host over streamable-HTTP on a background asyncio-loop thread.
# ---------------------------------------------------------------------------
class BackgroundServer:
    def __init__(self) -> None:
        self.server = build_server()
        # streamable_http_app() builds a Starlette app whose lifespan runs the
        # StreamableHTTPSessionManager. uvicorn drives that lifespan for us.
        app = self.server.streamable_http_app(
            streamable_http_path=PATH,
            host=HOST,
        )
        if REQUIRE_TOKEN:
            app = BearerAuthMiddleware(app, SECRET)
        self._uv = uvicorn.Server(
            uvicorn.Config(app, host=HOST, port=PORT, log_level="warning", lifespan="on")
        )
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._uv.serve())

    def start(self) -> None:
        self._thread.start()
        # wait until uvicorn reports started
        for _ in range(100):
            if getattr(self._uv, "started", False):
                return
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def stop(self) -> None:
        self._uv.should_exit = True
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 5. Client: connect over streamable-HTTP, list + call echo with progress.
# ---------------------------------------------------------------------------
async def run_client() -> None:
    progress_events: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        progress_events.append((progress, total, message))
        print(f"    [client progress] {progress}/{total}  {message!r}")

    headers = {"Authorization": f"Bearer {SECRET}"} if REQUIRE_TOKEN else None
    http_client = None
    if headers:
        from mcp.shared._httpx_utils import create_mcp_http_client

        http_client = create_mcp_http_client(headers=headers)

    # streamable_http_client yields a 2-tuple (read_stream, write_stream)
    async with streamable_http_client(URL, http_client=http_client) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"  initialized: server={init.server_info.name} v{init.server_info.version}")

            tools = await session.list_tools()
            print(f"  tools: {[t.name for t in tools.tools]}")
            print(f"  echo.inputSchema: {tools.tools[0].input_schema}")

            result = await session.call_tool(
                "echo",
                {"text": "hello v2"},
                progress_callback=on_progress,
            )
            texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
            print(f"  call_tool result: {texts}  is_error={result.is_error}")
            print(f"  progress notifications received: {len(progress_events)}")
            assert texts == ["echo: hello v2"], texts
            assert len(progress_events) == 3, progress_events
            print("  OK")


def main() -> None:
    print(f"require_token={REQUIRE_TOKEN}  url={URL}")
    srv = BackgroundServer()
    srv.start()
    print("server started on background thread")
    try:
        asyncio.run(run_client())
    finally:
        srv.stop()
        print("server stopped")


if __name__ == "__main__":
    main()

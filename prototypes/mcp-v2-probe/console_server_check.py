"""End-to-end check of runspec_console.mcp_server.ConsoleMcpServer against the real
v2 MCP client: serve on an ephemeral port, list tools (incl. a flattened
subcommand), call a fast tool, and a slow one to prove heartbeat progress fires."""

import asyncio
import socket
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import runspec_console.mcp_server as m


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = free_port()

RUNNABLES = [
    {
        "name": "greet", "host": "local", "group": "venv", "description": "say hi",
        "autonomy": "autonomous",
        "args": [{"name": "who", "type": "str", "required": True}],
    },
    {
        "name": "greeter", "host": "local", "group": "venv", "requireCommand": True,
        "autonomy": "confirm", "args": [],
        "commands": {
            "slow": {
                "name": "slow", "host": "local", "group": "venv",
                "description": "slow op", "autonomy": "confirm", "args": [],
            }
        },
    },
]


def execute(tool, arguments):
    if tool.command_path == ["slow"]:
        time.sleep(2.0)  # < shorten heartbeat below to force a tick
        return "slow done", False
    return f"hi {arguments.get('who')} [{tool.host}/{tool.runnable}]", False


def make_server():
    return m.ConsoleMcpServer(
        config_reader=lambda: {
            "enabled": True, "host": "127.0.0.1", "port": PORT,
            "token": None, "default_autonomy": None,
        },
        tools_provider=lambda: RUNNABLES,
        execute=execute,
    )


async def main():
    m._HEARTBEAT_S = 0.5  # force a heartbeat during the 2s slow call
    srv = make_server()
    srv.start()
    for _ in range(100):
        if srv.status().get("running"):
            break
        time.sleep(0.1)
    print("status:", srv.status())
    url = f"http://127.0.0.1:{PORT}/mcp"
    progress = []
    async with streamable_http_client(url) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("tools:", names)
            assert names == ["greet", "greeter_slow"], names
            res = await s.call_tool("greet", {"who": "ada"})
            print("greet ->", res.content[0].text, "is_error=", res.is_error)

            async def on_prog(p, total, msg):
                progress.append(msg)

            res2 = await s.call_tool("greeter_slow", {}, progress_callback=on_prog)
            print("slow ->", res2.content[0].text, "| progress ticks:", len(progress))
    srv.stop()
    assert len(progress) >= 1, "expected at least one heartbeat progress notification"
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())

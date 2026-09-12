"""Keep-alive prototype server — a streamable-HTTP MCP server whose one tool
simulates a *parked confirm*: it blocks for `seconds`, optionally emitting a
progress notification every `interval` seconds, then returns "approved".

This stands in for the real feature, where the block is `event.wait()` on the
console's confirm gate and the "approval" is a human clicking Approve.

Run:  PORT=8765 python server.py     (needs:  pip install "mcp>=1.0,<2")
Serves streamable-HTTP at  http://127.0.0.1:$PORT/mcp
"""

import os
import time

import anyio
from mcp.server.fastmcp import Context, FastMCP

PORT = int(os.environ.get("PORT", "8765"))
mcp = FastMCP("keepalive-proto", host="127.0.0.1", port=PORT)


@mcp.tool()
async def park(seconds: float, interval: float, ctx: Context) -> str:
    """Block for `seconds`, emitting progress every `interval` (0 = none)."""
    start = time.monotonic()
    sent = 0
    elapsed = 0.0
    while elapsed < seconds:
        step = seconds - elapsed
        if interval and interval < step:
            step = interval
        await anyio.sleep(step)
        elapsed = time.monotonic() - start
        if interval and elapsed < seconds:
            sent += 1
            await ctx.report_progress(
                progress=elapsed,
                total=seconds,
                message=f"waiting for operator approval… {elapsed:.1f}s",
            )
    return f"approved after {elapsed:.1f}s ({sent} progress notifications sent)"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

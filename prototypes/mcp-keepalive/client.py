"""Keep-alive prototype client — calls the server's `park` tool under a chosen
combination of the two *effective* timeout layers in mcp v1.30 streamable-HTTP:

  1. per-request `read_timeout_seconds` (ClientSession)  — a HARD ceiling.
  2. the httpx POST-response read timeout (default 300s) — RESET by incoming SSE
     bytes, i.e. by progress notifications.

(`streamablehttp_client(timeout=, sse_read_timeout=)` are deprecated / no-ops in
1.30, so we set layer 2 via a custom httpx client factory instead.)

Args:  park_seconds  progress_interval  read_timeout(0=none)  httpx_read(0=default 300)
"""

import os
import sys
import time
from datetime import timedelta

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared._httpx_utils import create_mcp_http_client


def _factory_with_read(read_s):
    def factory(headers=None, timeout=None, auth=None):
        return create_mcp_http_client(
            headers=headers, timeout=httpx.Timeout(30.0, read=read_s), auth=auth
        )
    return factory


async def run(url, park_seconds, interval, read_timeout, httpx_read):
    progress_cbs = 0

    async def on_progress(progress, total, message):
        nonlocal progress_cbs
        progress_cbs += 1

    kwargs = {}
    if httpx_read:
        kwargs["httpx_client_factory"] = _factory_with_read(httpx_read)

    async with streamablehttp_client(url, **kwargs) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            start = time.monotonic()
            try:
                res = await session.call_tool(
                    "park",
                    {"seconds": park_seconds, "interval": interval},
                    read_timeout_seconds=(
                        timedelta(seconds=read_timeout) if read_timeout else None
                    ),
                    progress_callback=on_progress,
                )
                dt = time.monotonic() - start
                text = res.content[0].text if res.content else ""
                print(f"  -> COMPLETED  elapsed={dt:5.1f}s  progress_cbs={progress_cbs}  :: {text}")
            except Exception as e:  # noqa: BLE001 - prototype: report any failure
                dt = time.monotonic() - start
                print(f"  -> FAILED     elapsed={dt:5.1f}s  progress_cbs={progress_cbs}  :: {type(e).__name__}: {e}")


if __name__ == "__main__":
    url = os.environ["URL"]
    ps, iv, rt, hr = (float(x) for x in sys.argv[1:5])
    anyio.run(run, url, ps, iv, rt, hr)

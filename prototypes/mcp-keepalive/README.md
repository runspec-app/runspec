# Prototype: MCP "parked confirm" keep-alive

Spike for the **console-as-MCP-server** feature (see
`docs/design/console-mcp-server.md`). It answers the one load-bearing risk:

> When an external MCP client (Claude Code) calls a `confirm`-level runnable and
> the console **parks** the call awaiting a human's Approve, can the MCP
> `tools/call` stay alive across a human-scale wait — or does a client timeout
> abort it first?

This does **not** touch the console. It's a standalone streamable-HTTP MCP server
whose one `park` tool blocks for N seconds (standing in for `event.wait()` on the
confirm gate), optionally emitting a progress notification every `interval`
(standing in for a heartbeat), then returns "approved". A client calls it under
different timeout settings and we observe what survives.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install "mcp>=1.0,<2"
PYTHON=.venv/bin/python ./run.sh
```

## What was tested — MCP Python SDK **1.30.0**, streamable-HTTP, localhost

There are two *effective* timeout layers in 1.30 (the `streamablehttp_client`
`timeout=`/`sse_read_timeout=` params are **deprecated / no-ops** in this
version — confirmed in `mcp/client/streamable_http.py`):

1. **Per-request `read_timeout_seconds`** (passed to `ClientSession.call_tool`) —
   implemented as `anyio.fail_after(timeout)` around the response wait
   (`mcp/shared/session.py` ~L291). A **hard deadline**; progress notifications
   are handled on a separate path and do **not** reset it.
2. **The httpx read timeout on the POST response stream** — default **300s**
   (`create_mcp_http_client`, "because a server may hold a response stream
   open"). This one **is** reset by incoming SSE bytes — i.e. by progress
   notifications.

### Results (measured)

| # | per-request read_timeout | progress | httpx read | outcome | elapsed |
|---|---|---|---|---|---|
| A | 3s | every 1s | default | **FAILED** (McpError timeout) | 3.0s |
| B | none | every 1s | default (300s) | **COMPLETED** | 8.0s |
| C | none | none | 3s | aborted (then client teardown stalled to the 25s shell guard) | — |
| D | none | every 1s | 3s | **COMPLETED** | 8.0s |

### Reading

- **A** — a per-request `read_timeout_seconds` is a **hard ceiling**. Two progress
  notifications arrived, and it still died at exactly 3s. Progress cannot rescue a
  call whose client set a short per-request timeout.
- **B** — with no per-request timeout, an 8s park completes normally under the
  default 300s httpx window.
- **C vs D** — the decisive pair. Same tight 3s httpx read window; the only
  difference is progress. **No progress → the parked call does not survive**
  (aborts at the read boundary; note the client's error-path teardown then stalled
  until the shell guard — a cleanliness issue to handle in the real impl). **With
  progress every 1s → it COMPLETES at 8s**: each SSE notification reset the httpx
  read timeout.

## Verdict for the feature

**Keep-alive works — with a precise contract and one external dependency.**

A parked-confirm `tools/call` survives a long human wait **iff**:

1. the server emits **heartbeat progress notifications** while parked, at an
   interval well under the httpx read window (e.g. every 15–30s) — *server-side,
   we control this*; **and**
2. the client has **not** set a shorter per-request `read_timeout_seconds` — *this
   is the client's (Claude Code's) choice, which the server cannot override.*

Because of (2), the real implementation should **not** rely on blocking
indefinitely. Recommended design:

- Park + heartbeat, block up to a **bounded** window (≤ the console's
  `confirm_timeout`, default 300s — which happily matches the httpx read default).
- Approved in time → run and return the result (the common case: approvals happen
  in seconds → one clean call).
- Not approved in time → return a **graceful, resumable** result ("⏳ awaiting
  operator approval in the console; request `<id>`") rather than an error. The
  confirm stays parked in the console; on approval the run executes and files to
  History, and the agent can retry / be resumed. This makes us robust to whatever
  per-request timeout Claude Code imposes.

## Verification against the REAL Claude Code CLI

Re-run end-to-end with `claude` 2.1.268 driving the same server over an `http`
MCP config (`claude-mcp.json`), via `./claude-verify.sh`. Claude Code exposes a
per-tool-call timeout env var **`MCP_TOOL_TIMEOUT` (default 60000ms = 60s)**.

| test | park | MCP_TOOL_TIMEOUT | outcome | wall |
|---|---|---|---|---|
| T1 | 20s | 60000 (default) | **COMPLETED** — "approved after 20.0s (3 progress sent)" | 31s |
| T2 | 90s | 60000 (default) | **`Error: MCP server "keepalive" tool "park" timed out after 60s`** | 70s |
| T3 | 90s | 120000 | **COMPLETED** — "approved after 90.0s (17 progress sent)" | 104s |

**This confirms the SDK-level finding against the real client:**

- Claude Code's `MCP_TOOL_TIMEOUT` is a **hard per-tool-call ceiling** — T2's 90s
  park died at exactly 60s **despite 12 progress notifications** having been sent.
  Progress does **not** reset Claude Code's tool timeout (it's the SDK's
  `read_timeout_seconds` / `anyio.fail_after` deadline).
- The ceiling is **client-configurable**: raising `MCP_TOOL_TIMEOUT` to 120s let
  the identical 90s park complete (T3).
- Parks comfortably under the ceiling just work (T1).

### Design consequence (now grounded against the real client)

1. **Heartbeat progress does not help against Claude Code's tool timeout** — that
   timeout is a hard ceiling. (Heartbeats still matter only for the httpx 300s
   transport layer, i.e. *if* `MCP_TOOL_TIMEOUT` is ever raised above 300s.)
2. The **bounded-wait-then-resumable** design is therefore **required**: the park
   handler must return a graceful "⏳ awaiting operator approval, request `<id>`"
   result before the client's tool timeout fires (bound ≈ `MCP_TOOL_TIMEOUT`
   minus margin; default ~55s), leaving the confirm parked in the console. On
   approval the run executes and files to History; the agent retries / is resumed.
3. **Optional smoother UX:** the shipped plugin / MCP config can raise
   `MCP_TOOL_TIMEOUT` (e.g. to 300s) so the common "approve within a couple of
   minutes" case is one clean blocking call — but the resumable path stays as the
   backstop, since there is always *some* ceiling.

### Caveats / not covered

- **SDK v1 vs v2 (resolved):** this prototype used mcp v1 (1.30). The console's
  own `mcp_client.py` is already on the **v2** API (`streamable_http_client`,
  migrated in #565), so the console builds its MCP *server* against v2 too and
  pins the extra to `mcp>=2,<3`. The timeout semantics measured here were
  re-confirmed against the real Claude Code client (above), so they carry over.
- Error-path teardown of `streamablehttp_client` stalled in scenario C — wrap
  server-side waits and client teardown with their own timeouts in the real impl.

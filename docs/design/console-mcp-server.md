# Console as an MCP server — routing an external coding agent through the console's confirm gate

**Status:** design / feasibility confirmed by a code-map pass. Not yet built.
Companion to `console-ai-off-switch.md` (independent, deferred).

---

## Goal

Let a **running** runspec-console expose an in-process **MCP server** on
`127.0.0.1` so an external coding agent (Claude Code, or any MCP client) connects
to it and can run the console's fleet runnables (local + remote venvs). When the
agent calls a runnable whose autonomy is `confirm`/`supervised`, the call **parks
in the console's existing confirm-card UI** and blocks until a human
approves / edits / denies. `autonomous` runs straight through; `manual` is
refused. Both agent-initiated and human-initiated runs land in the **same
History/audit trail**.

This is the "park-and-block" governance gate — but nearly free, because the
console already owns the confirm UI, the SSH fleet, credentials, discovery, and
the audit trail. It turns the console into the approval surface for an external
agent's fleet calls, with (optionally) no AI running *inside* the console.

Not always-on: the endpoint is up only while the console window is open, which is
the intended UX (open the console when you want the GUI and/or governed agent
fleet access).

---

## Architecture

```
Claude Code ──HTTP MCP (127.0.0.1:PORT, optional token)──▶ console (pywebview app)
                                                            │
   confirm-card UI ◀── _register_confirm / event / _finish ─┤  confirm/supervised → PARK + block
   resolve_tool_confirmation(request_id, approved, edited) ─▶│  autonomous → run now
                                                            │  manual → refuse (MCP error)
   History (get_history_all) ◀── executor + run_origin ─────┘  execute; tag provenance
```

Two threads, one `Bridge` object (mixin composition → all shared state reachable):
- **MCP server** runs on a dedicated background asyncio loop thread (the
  `McpManager` pattern), brought up from `app.py`.
- **UI** thread receives confirm cards via the existing serialized `_dispatch`
  queue; the human's click calls back into `resolve_tool_confirmation`.

---

## The confirm gate is caller-agnostic (the key finding)

`bridge_autonomy.py` already exposes the exact park/await/cleanup trio, and it is
not welded to the agent loop — it takes a plain `chat_id` string used only to
route the UI event and allow cancellation:

- `_register_confirm(chat_id, tool_name, tool_input, level) -> (request_id, threading.Event, holder)`
  — mints `request_id = uuid4().hex`, stores `self._pending_confirms[request_id]`
  and a display record `self._pending_confirm_meta[request_id]`, and pushes
  `_dispatch("runspec:tool_confirm", {...})` to the frontend (+ OS toast/sound via
  `_notify_confirm`). `display_name`/`display_input` are the human view; raw
  `tool_name`/`tool_input` stay so the card matches the running call.
- **await:** `await asyncio.to_thread(event.wait, self._confirm_timeout_s(headless))`
  (mirrors `_gated_run_tool_async`). `_confirm_timeout_s` → `[console]
  confirm_timeout` (300s chat) / `trigger_confirm_timeout` (3600s unattended);
  `0` = wait forever.
- `_finish_confirm(request_id, holder, timed_out) -> (approved, edited_input|None)`
  — pops both dicts, emits `_dispatch("runspec:confirm_resolved", {...})`
  (authoritative "gate done", covers timeout/cancel the UI never answered).
- Frontend callback `resolve_tool_confirmation(request_id, approved, edited_input=None)`
  (already JS-callable + in the frozen API) sets the holder + `event.set()`.
- Cancellation: `cancel_chat(chat_id)` walks `_pending_confirms`, sets
  `holder["cancelled"]=True` + `event.set()` — so an MCP call parked under a
  known id can be woken/cancelled the same way.
- Cold-start: `get_pending_confirms()` returns the parked set (work-inbox seed) —
  surfaces a parked *external* gate too, no change needed.

**So the MCP server handler does exactly what the async gate does**, minus the
agent-loop bookkeeping:

```
level = self._effective_tool_autonomy(tool_name, tool_input)   # or force "confirm"
if level == "manual": return mcp_error(...)                    # refuse
if level != "autonomous":
    rid, event, holder = self._register_confirm(mcp_id, tool_name, tool_input, level)
    await asyncio.to_thread(event.wait, self._confirm_timeout_s(headless=True))
    approved, edited = self._finish_confirm(rid, holder, timed_out=not event.is_set())
    if holder["cancelled"] or not approved: return mcp_error("declined")
    tool_input = edited or tool_input
# execute (below) and return the result as MCP text content
```

Edit-before-approve works for **direct** calls (the `edited_input` dict replaces
`tool_input`); `_confirm_card_view` renders wrapper tools read-only, but MCP
runnable calls are direct, so edits apply.

---

## Transport & schema generation

- **Transport:** the MCP Python SDK (already the `mcp>=1.0` extra) ships the
  server side incl. streamable-HTTP (`mcp.server` / `mcp.server.streamable_http`,
  Starlette-based). Host it on the background loop thread — **no new
  Starlette/uvicorn console dependency** (that dep lives only in the separate
  `runspec-room` package; don't pull it in). Gate the feature behind the existing
  `mcp` extra.
- **Schemas — reuse, don't reinvent:** build tool schemas from
  `runspec.serve._expand_tools` + `runspec.cli._build_schema` (+ the
  `runspec-mcp/runspec_mcp/catalog.py` `Tool`/`Catalog` live-registry pattern):
  subcommands flattened to leaf tools, `x-autonomy` carried, **`password` args
  omitted** (secrets never reach argv/agent schemas). This makes the console's
  MCP tools byte-identical to what `runspec serve` / `runspec-mcp` advertise.
- **Do NOT reuse** the stdio JSON-RPC framing in `runspec-mcp/.../server.py` or
  core `serve.py` (`_write`→stdout, hand-rolled `serve_forever`) — that's
  transport-specific. Reuse only their discovery/expansion parts.

---

## Execution + provenance (unified History)

There is **no console-side DB write** for runnable runs — the audit `run_summary`
is written by the runspec **core process** (the runnable itself) into its log
file. The console passes provenance as **environment variables** the core reads
into `run_summary.extra`:

- `agent=True` → `RUNSPEC_AGENT=1` → record `initiatedBy="llm"`.
- `trigger="mcp"` → `RUNSPEC_TRIGGER=mcp` → distinguishes agent-over-MCP from the
  console's own chat and from other event sources.

So before running, set an origin and invoke:

```
self._set_run_origin(mcp_id, trigger="mcp", agent=True)   # bridge_autonomy
result = invoke_runnable(host, runnable, args, command_path=..., operator=..., trigger="mcp")
# or _run_tool_sync(...) for the agent-shaped path
self._clear_run_origin(mcp_id)
```

The run then appears in `get_history_all()` with `initiatedBy="llm"`,
`trigger="mcp"`, correct `host`/`operator`/`runAs`/`exitCode`/`args`. Control /
non-runnable MCP tools (e.g. a `fleet_hosts` helper) that have no core
`run_summary` should be audited via the `_audit_builtin_tool(...)` pattern
(`~/logs/runspec-console-tools.log`).

Execution paths available: `invoke_runnable(...)` (Forms/human-shaped; `agent`
defaults False, password args auto-split into the secret env channel) and
`_run_tool_sync(...)` (agent-shaped; resolves credentials *into* args and reads
`_run_origin`). Choose one and set provenance explicitly either way.

---

## Autonomy mapping (external agent)

| runspec autonomy | over the console MCP server |
|---|---|
| `autonomous` | run immediately, no park |
| `confirm` / `supervised` | **park in the console confirm card**, block until human resolves |
| `manual` | refuse (MCP error) — operator-only |
| unknown / unresolved | treated as `confirm` (fail-closed, per `_effective_tool_autonomy`) |
| `password` args | omitted from the tool schema entirely |

Denylist and `enforce_run_as` still apply at core `parse()` time on the executing
host, regardless of the gate.

---

## Locked decisions

- **Endpoint security.** Localhost-only (`127.0.0.1`) **plus an optional bearer
  token** in `[mcp_server]`. Optional in v1 (localhost is the primary bound); when
  set, required on every request. Token written to the discovery file below.
- **Port.** **Configurable** with a sensible default, written to a **discovery
  file** (in the app dir) that Claude Code's MCP config / the plugin points at, so
  the client doesn't hard-code a port.
- **Enable + default.** New `[mcp_server]` config (enabled / port / token /
  default autonomy) + a Settings toggle. **Off by default** — it opens a listening
  socket, so it's opt-in.
- **Execution path.** Use **`invoke_runnable(...)`** (the operator/Forms shape:
  password args auto-split into the secret env channel, `agent` defaults False so
  we set provenance explicitly). This fits the "governed front door" framing
  better than the agent-shaped `_run_tool_sync` (which injects credentials *into*
  args). Provenance set via `_set_run_origin(id, trigger="mcp", agent=True)` before
  the call.
- **Relationship to `runspec-mcp`.** The standalone stdio gateway stays the
  headless option; this is the *governed* front door. Share `_expand_tools` /
  `_build_schema` / the `catalog` pattern; don't fork execution.
- **Frozen API test.** Any new JS-callable bridge method (`mcp_server_start`,
  `mcp_server_status`, `save_mcp_server_config`, …) must be added to `EXPECTED`
  in `tests/test_bridge_api_surface.py`. Internal hooks (`_register_confirm`
  etc.) are private and don't touch it.

## The one risk — prototyped, resolved

**MCP request timeout across a human-scale wait.** Prototyped under
`prototypes/mcp-keepalive/` against MCP Python SDK 1.30.0, streamable-HTTP,
localhost. **Verdict: keep-alive works, with a precise contract.**

Two effective timeout layers (the transport `timeout`/`sse_read_timeout` params
are deprecated no-ops in 1.30):

1. **Per-request `read_timeout_seconds`** (client-set, `ClientSession.call_tool`)
   — a **hard ceiling** (`anyio.fail_after`); progress does **not** reset it.
   Measured: with `read_timeout=3s`, an 8s park died at 3.0s despite progress.
2. **httpx POST-stream read timeout** — default **300s**, and **reset by incoming
   SSE bytes** (progress notifications). Measured: at a tight 3s read window, an
   8s park with progress every 1s COMPLETED; with no progress it aborted.

**Verified against the real Claude Code CLI** (2.1.268, not just the SDK client —
`prototypes/mcp-keepalive/claude-verify.sh`). Claude Code has a per-tool-call
timeout **`MCP_TOOL_TIMEOUT`, default 60000ms**, which it passes as the SDK's hard
`read_timeout_seconds`:

- park 20s @ 60s timeout → **completed**;
- park 90s @ 60s timeout → **`timed out after 60s`** despite 12 progress
  notifications (progress does **not** reset Claude Code's tool timeout);
- park 90s @ 120s timeout → **completed** (ceiling is client-configurable).

**Design consequence — don't block indefinitely; heartbeats don't beat the tool
timeout.** Against Claude Code's `MCP_TOOL_TIMEOUT` the timeout is a hard ceiling —
heartbeat progress only matters for the httpx 300s transport layer (i.e. *if*
`MCP_TOOL_TIMEOUT` is raised above 300s). So the handler parks up to a **bounded**
window ≈ `MCP_TOOL_TIMEOUT` minus margin (default ~55s); approved in time → run +
return (common case: one clean call); not in time → return a **graceful,
resumable** result ("⏳ awaiting operator approval, request `<id>`") rather than an
error, leaving the confirm parked in the console. On approval the run executes and
files to History; the agent retries / is resumed. This removes the dependence on
the client's timeout entirely. **Optional smoother UX:** the shipped plugin / MCP
config can raise `MCP_TOOL_TIMEOUT` (e.g. 300s) so the common approve-within-a-
couple-of-minutes case is one clean blocking call — with the resumable path as the
backstop, and heartbeat progress then needed to hold the transport open.

SDK version (resolved): the console's `mcp_client.py` is **already on the v2 API**
(`streamable_http_client(url, http_client=…)`, migrated in #565) — an earlier note
here calling it "v1-shaped" was wrong. This feature builds the **server** against
the same v2 SDK and pins the extra to `mcp>=2,<3` (the SDK breaks across majors, so
cap it). The keep-alive prototype under `prototypes/mcp-keepalive/` was run against
v1 (1.30) but the timeout *semantics* it measured were re-confirmed end-to-end
against the real Claude Code client (see that README), so the finding stands.
Remaining follow-up: wrap server-side waits + client teardown in their own timeouts
(an error-path teardown stall was seen in the SDK-client run).

---

## Build sequence (proposed)

1. **Prototype the timeout/keep-alive** (risk #1) — a single hard-coded `confirm`
   tool over streamable-HTTP that parks via `_register_confirm` and returns after
   a real human approval, under Claude Code's live timeout. Prove keep-alive.
2. **Server host thread** — copy the `McpManager` start/`_run_loop`/stop shape;
   bring up from `app.py` next to `bridge.mcp_start()`; `atexit` stop; behind the
   `mcp` extra; off by default.
3. **Schema registry** — wire `_expand_tools` + `_build_schema` + a `Catalog`-like
   live registry (reuse `runspec-mcp/catalog.py` as template).
4. **Gated execution** — the handler in "confirm gate is caller-agnostic" above;
   provenance via `_set_run_origin(trigger="mcp", agent=True)`.
5. **Config + Settings tab + token + discovery file**; add bridge methods to the
   frozen API test.

---

## Key file:symbol reference

- Confirm gate: `bridge_autonomy.py` — `_effective_tool_autonomy` (~L98),
  `_confirm_decision` (~L264), `_register_confirm` (~L273), `_finish_confirm`
  (~L758), `_confirm_timeout_s` (~L253), `_gated_run_tool_async` (~L796; the
  template). Frontend callback `resolve_tool_confirmation` (~L1304).
  `get_pending_confirms` in `bridge_triggers.py` (~L2052).
- State: `bridge.py` ~L219 (`_pending_confirms`, `_pending_confirm_meta`),
  `_dispatch`/`_ui_dispatch_loop` (~L281/L287), `cancel_chat` in
  `bridge_invoke.py` (~L702).
- Execution: `invoke_runnable` (`bridge_invoke.py` ~L125), `_run_tool_sync`
  (`bridge_autonomy.py` ~L1335), provenance `_set_run_origin`/`_clear_run_origin`
  (~L1241/L1279); `executor.py` `run_local`/`run_remote` (~L330/L576),
  `prov_env` (~L378).
- History: `get_history` / `get_history_all` (`bridge_reporting.py` ~L102/L228),
  record fields in `bridge_utils.py` `_parse_log_by_run_id` (~L473),
  `_audit_builtin_tool` (`bridge_reporting.py` ~L47).
- Async-thread pattern: `mcp_client.py` `McpManager` (~L300) start/`_run_loop`/
  `call_tool`/stop; bridge glue `bridge_agent_tools.py` `mcp_start` (~L2519).
  App bring-up `app.py` `main()` (~L383 `Bridge()`, ~L399 `mcp_start()`).
- Schema reuse: `runspec/serve.py` `_expand_tools` (~L416), `runspec/cli.py`
  `_build_schema` (~L1167, omits `password`), `runspec-mcp/catalog.py`
  `_build_host_tools` (~L139), `Tool`/`Catalog` (~L37/L207).
- Frozen API: `runspec-console/tests/test_bridge_api_surface.py` `EXPECTED`
  (~L16), `test_public_api_surface_is_frozen` (~L205).
- Dep note: console core has no Starlette/uvicorn; MCP SDK server rides the
  existing `mcp>=1.0` extra (`pyproject.toml` ~L85).

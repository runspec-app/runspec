# Console as an MCP server

The runspec-console is normally an MCP *client* (see
[External MCP servers](console-mcp.md)). This is the mirror: the **running
console hosts its own MCP server** on localhost, so an external coding agent —
**Claude Code**, Cursor, Cline, or any MCP client — can run the console's fleet
runnables as tools, with the console's confirm gate in the loop.

```
Claude Code ──HTTP MCP (127.0.0.1:PORT)──▶ runspec-console (running)
   confirm card ◀── park & approve ─────────┤  autonomous → runs now
   (you click Approve in the console)        │  confirm    → parks in the console UI
   History (agent + human runs) ◀────────────┘  manual     → refused
```

Why route the agent *through* the console rather than a standalone gateway
([`runspec-mcp`](mcp.md))? Because the console already owns the SSH fleet,
credentials, discovery, the **confirm-card UI**, and the History/audit trail — so
a `confirm`-level call from the agent surfaces as the *same* approval card the
console's own agent uses, and every run (agent- or human-initiated) lands in one
History. It's the governed front door.

Requires the `mcp` extra: `pip install runspec-console[mcp]`.

---

## Enable it

**Off by default** — it opens a listening socket, so it's opt-in.

Settings → **MCP Server**:

- **Enable server** — starts it (applies immediately).
- **Host** — `127.0.0.1` (loopback is the primary security bound; changing it off
  loopback is not recommended).
- **Port** — default `8765`. The actual value is written to a discovery file (see
  below) so the client's config need not hard-code it.
- **Bearer token** *(optional)* — when set, required on every request
  (`Authorization: Bearer <token>`). Localhost is the primary bound; the token
  guards against other local processes on the machine.
- **Default autonomy floor** *(optional)* — force every tool to at least this
  level (most-restrictive wins). Unset ⇒ each runnable's own declared autonomy.
- **Confirm wait** — how long a parked call waits for your approval before
  returning a resumable result (see [Autonomy](#autonomy-the-confirm-gate) below).

Or in `config.toml`:

```toml
[mcp_server]
enabled = true
host = "127.0.0.1"
port = 8765
# token = "…"              # optional bearer token
# default_autonomy = "confirm"
confirm_wait = 50
```

`[mcp_server]` lives in `config.toml`, so it rides Config Sync / the config seed
like every other section (the token stays local — set it per machine).

---

## Connect Claude Code

While the console is running it writes a discovery file at
`{app_dir}/runspec_mcp_server.json` with the live URL (and token, when set):

```json
{ "url": "http://127.0.0.1:8765/mcp", "token": "…", "header": "Authorization: Bearer …" }
```

Add it to Claude Code (the Settings tab shows the exact command):

```bash
claude mcp add --transport http runspec http://127.0.0.1:8765/mcp
# with a token:
claude mcp add --transport http runspec http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <token>"
```

Any MCP client that speaks streamable-HTTP works the same way. The console must be
**running** for the endpoint to be up — this is a desktop, not a daemon; open the
console when you want the agent to reach the fleet.

The tools are **serve-identical** to `runspec serve` / [`runspec-mcp`](mcp.md):
subcommands are flattened to `parent_child` leaf tools, `password` args never
appear in a tool schema, and each tool carries its runnable's autonomy. The same
runnable discovered on several fleet hosts is exposed once per host (the extra
hosts get a `<host>__` prefix). `[tool_visibility]` hides carry over (a runnable
hidden from the agent is not published here either).

---

## Autonomy — the confirm gate

Each tool is gated by its runnable's declared `autonomy` (escalated by any
per-arg autonomy and the optional floor):

| autonomy | over the console's MCP server |
|---|---|
| `autonomous` | runs immediately, no prompt |
| `confirm` / `supervised` | **parks in the console's confirm card** — you Approve / edit args / Deny |
| `manual` | refused (operator-only; run it yourself in the console) |
| `password` args | omitted from the tool schema entirely |

The denylist and `enforce_run_as` still apply on the executing host, regardless.

**Parked calls and the client timeout.** An MCP client imposes its own per-tool
timeout (Claude Code's `MCP_TOOL_TIMEOUT` defaults to **60s**), and progress
notifications do **not** extend it. So a parked `confirm` waits only up to
`confirm_wait` (default **50s**, deliberately under that ceiling): if you approve
in time, the runnable runs and its output returns; if not, the tool returns a
graceful, **resumable** message —

> ⏳ Awaiting operator approval in the console (request `ab12cd34`). Approve it
> there, then call this tool again to run it.

— and the parked card clears. Nothing runs late or orphaned; the agent simply
calls again (raising a fresh confirm) once you've approved. If you raise the
client's `MCP_TOOL_TIMEOUT`, raise `confirm_wait` to match for a smoother
single-call approval.

---

## History & provenance

Agent-initiated runs execute through the same executor as Forms runs and are
tagged `trigger = "mcp"`, so the History tab shows them alongside human runs and
you can tell which came from the coding agent. Secrets are never audited (as
everywhere).

---

## See also

- [External MCP servers](console-mcp.md) — the console as an MCP *client*.
- [`runspec-mcp`](mcp.md) — the standalone (headless) gateway; this is the
  governed, in-console alternative.
- `docs/design/console-mcp-server.md` — the design note (confirm-gate reuse, the
  keep-alive prototype + real-Claude-Code verification, the file:symbol map).

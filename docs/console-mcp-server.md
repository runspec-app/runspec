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
- **Tool style** — how runnables with subcommands are presented (see
  [Tool style](#tool-style-leaf-vs-per-runnable) below). `leaf` (default) is
  serve-identical; `runnable` collapses each runnable to one tool with a
  `command` argument.

Or in `config.toml`:

```toml
[mcp_server]
enabled = true
host = "127.0.0.1"
port = 8765
# token = "…"              # optional bearer token
# default_autonomy = "confirm"
confirm_wait = 50
# tool_style = "leaf"      # or "runnable" — one tool per runnable + a command arg
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

When a bearer token is set it's written here in the clear, so the file is created
**owner-only (0600)** — see [Security & the token](#security-the-token) below.

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

On a white-label build, the server advertises its `[console.theme] brand_name` as
the protocol-level server name a connecting client sees (falling back to
`runspec-console` when no brand is set), so the coding agent's server listing
matches the brand.

The tools are **serve-identical** to `runspec serve` / [`runspec-mcp`](mcp.md):
subcommands are flattened to `parent_child` leaf tools, `password` args never
appear in a tool schema, and each tool carries its runnable's autonomy. The same
runnable discovered on several fleet hosts is exposed once per host (the extra
hosts get a `<host>__` prefix). `[tool_visibility]` hides carry over (a runnable
hidden from the agent is not published here either).

---

## Tool style — `leaf` vs per-runnable

A fleet of multi-verb runnables can expose a **lot** of leaf tools: `jira` alone
flattens to ~37 (`jira_get-issue`, `jira_create-issue`, …), `confluence` ~20,
`gitlab` ~17 — and **every host** the runnable is installed on multiplies that
again (5 hosts × `runspec-linux`'s ~31 runnables = a lot). Serve-identical, that's
how `runspec serve` presents them — but some MCP clients struggle with hundreds of
tools in one list. `tool_style` picks the shape:

| `tool_style` | what the client sees | when |
|---|---|---|
| `leaf` (default) | one tool per subcommand **per host** — `jira_get-issue`, …, and `<host>__`-prefixed copies for extra hosts (serve-identical) | precise per-verb arg schemas; fine when the client handles many tools |
| `runnable` | one tool per **distinct** runnable — `jira` with a `command` argument (the verbs) and, when it's on more than one host, a `host` argument (the hosts) | far fewer tools; matches the console's own deduped view; use when a client chokes on a big list |

`runnable` style collapses **both** multiplying dimensions:

- **Subcommands** → a `command` enum argument. The runnable's own (top-level) args
  are typed; per-subcommand args ride through as additional properties (validated
  by the executor at run time).
- **Hosts** → the same runnable discovered on several hosts is **one** tool with a
  `host` enum argument (required when it's on >1 host; absent for a single-host
  runnable). So 5 hosts × 31 runnables is ~31 tools, not ~155 — matching the count
  the console shows.

**Autonomy is still resolved per chosen subcommand** — picking `create-issue` gates
exactly as the `jira_create-issue` leaf tool would, while `get-issue` stays
autonomous — so the confirm gate behaves identically to `leaf`; only the tool
*count* changes.

Another lever for a large fleet is `[tool_visibility] hidden_from_agent` — hide
whole packages/groups you don't need over MCP (the hides carry over to this
server). Combine both: collapse to `runnable` style and hide what you don't use.

### Live tool list (discovery)

The server advertises `tools.listChanged` and pushes a
`notifications/tools/list_changed` to the connected client whenever the console's
fleet discovery updates. So if you open the console (or reconnect the client)
while it's still connecting to remote hosts, the client refreshes to the full set
on its own as discovery completes — no manual reconnect needed. (A client that
ignores the notification can still re-list manually.)

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

## Credentials

Runnables that need a secret (a Jira PAT, a service-account password, an API
token) get it **automatically, by scope** — the same model as the console's own
agent. You never hand a credential to the MCP client, and the client never sees a
secret:

- Credentials are matched to a run by their **scope** (`scope_hosts` /
  `scope_runnables` globs, set in Settings → Credentials; an empty scope matches
  everything). Every in-scope credential for the `(host, runnable)` is applied
  server-side, its secret read from the OS keychain at run time.
- **Both delivery channels fire.** The **name-derived env vars** (a `Jira`
  userpass credential → `JIRA_USERNAME`/`JIRA_PASSWORD`; a `Deploy` token →
  `DEPLOY_TOKEN`/`DEPLOY_PAT`) for runnables that read the environment, **and**
  the **arg-bindings** (`username_arg` → the argument, `secret_arg` → the secret
  env channel) for runnables whose interface is `--user`/`--password` args — so
  both styles of runnable work over MCP.
- `password`-typed args are omitted from the tool schema entirely, so the agent
  can neither see nor supply them — the console fills them from the credential.

To make a credential available to MCP runs, scope it to the host(s)/runnable(s) it
should serve (or leave the scope empty to apply it everywhere). A runnable with no
matching credential simply runs without one (and fails auth if it needed it).

Two deliberate limits versus the in-console chat agent:

- **No per-call credential selection.** The chat agent can name a `credential_id`;
  an external MCP client can't (the tool schema exposes no such field). If two
  credentials could match one runnable, tighten their scopes.
- **No sole-password fallback.** Filling a lone `password` arg from a single
  matching credential *without* a `secret_arg` binding is chat-agent-only; over
  MCP, bind it explicitly with `secret_arg` (or use the env-var channel).

## Security & the token

The server binds to **loopback** (`127.0.0.1`), which is the primary boundary:
loopback traffic never touches a network interface, so there's no network
man-in-the-middle to worry about, and the optional **bearer token** guards against
*other local processes* connecting to the port. That's why plain HTTP is fine here
and TLS isn't needed on loopback (a client that *demands* `https://` is better
served by a local stdio bridge than by adding a cert the client won't trust).

Given that, the bearer token's real exposure is **at rest**, not on the wire — so:

- The **discovery file** (`{app_dir}/runspec_mcp_server.json`) is written
  **owner-only (0600)**: on POSIX the permissions are set before the token is
  written (no readable window) and an older looser file is tightened; on Windows
  the per-user app directory's ACLs govern.
- The token is **never** written to `config.toml`'s synced copy in a way that
  leaves Config Sync — set it per machine (`[mcp_server] token`), it stays local.
- Runnable **secrets** (credential-supplied passwords/tokens) never travel to the
  client and are never audited — see [Credentials](#credentials) above.

If you don't run other untrusted local processes, the token is optional; set one
when you want to be sure only your own client can reach the port.

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

# External MCP servers

The console is normally an MCP **server** producer: `runspec serve` publishes
each runnable as a leaf MCP tool for an outside agent to call. This page is about
the other direction — making the console's chat agent an MCP **client**, so tools
published by an *external* MCP server show up in the agent's toolset alongside
your runnables and the built-in tools.

Declare the servers in `config.toml` under `[mcp]`; the console connects to each
at launch and folds its tools in. External tool calls run through the same
autonomy / confirm gate as everything else, and — like Telemetry and Phoenix —
the feature is behind an opt-in extra.

## Install the extra

```
pip install "runspec-console[mcp]"
```

Without it, any configured servers are simply skipped (the Settings hint and
`mcp_status` report the SDK is missing). It's also part of
`runspec-console[all]`.

## Configure servers

The easiest way is **Settings → MCP**: a tab that lists your servers with a live
connection status (green *connected · N tools* / red *error* with the reason /
*disabled*), an **Add**/edit dialog for each, a **Reconnect** button, a section
on/off switch, and the connect/call timeouts. The edit dialog has the transport
picker, the
transport-specific fields (command/args/env or url/headers), a **credential**
dropdown (from your Credentials tab), the credential-header override, the proxy,
per-server timeout overrides, and the autonomy level. Saving validates the entry,
writes `config.toml`, and reconnects immediately. Each row also has an inline
**Enabled** switch, so a server can be toggled without opening its editor.

Everything the tab writes is plain `config.toml`, so you can also hand-edit it.
Each server is a `[[mcp.servers]]` entry. Three transports are supported:

```toml
[mcp]
# enabled = true            # section kill switch (default true)
# call_timeout = 60         # default seconds per tool call (all servers)
# connect_timeout = 20      # default seconds to wait for the initial connect (all servers)

# A local server started as a subprocess (stdio).
[[mcp.servers]]
name = "files"
transport = "stdio"
command = "uvx"
args = ["mcp-server-filesystem", "/srv/data"]
# env = { TOKEN = "…" }     # added to the child's environment
# autonomy = "confirm"      # confirm (default) | autonomous | manual

# A remote server over streamable-HTTP, authenticated from a named credential
# and reached through this network's proxy.
[[mcp.servers]]
name = "search"
transport = "http"
url = "https://mcp.example.com/mcp"
credential = "cred-search-token"   # keychain-backed; becomes Authorization: Bearer
proxy = "http://proxy.corp:8080"   # this server's egress (see "Per-server proxy")
call_timeout = 300                 # override just this server's per-call deadline
# connect_timeout = 45             # …and/or its connect wait; omit to inherit [mcp]

# A remote server over SSE with a custom auth header.
[[mcp.servers]]
name = "wiki"
transport = "sse"
url = "https://wiki.example.com/sse"
credential = "cred-wiki"
credential_header = "X-API-Key"    # send the secret raw under this header
```

Fields:

| Field | Applies to | Meaning |
|---|---|---|
| `name` | all | Short id; **required**. Namespaces the tools (`mcp_<name>_<tool>`). |
| `transport` | all | `stdio`, `http` (streamable-HTTP), or `sse`. Inferred from `command`/`url` if omitted. |
| `command` / `args` / `env` / `cwd` | stdio | The subprocess to spawn. `env` is added on top of the console's environment. |
| `url` / `headers` | http, sse | The server endpoint and any auth headers. |
| `enabled` | all | Per-server toggle (default true). Also an inline switch on the row. |
| `call_timeout` / `connect_timeout` | all | Per-server overrides (seconds) of the section defaults above. A positive value overrides just this server's per-call / connect deadline; omit (or 0) to inherit the `[mcp]` default. Lets one slow server get a longer leash without loosening the fleet. |
| `autonomy` | all | `confirm` (default), `autonomous`, or `manual` for this server's tools. |
| `credential` | all | A named-credential id (Settings → Credentials) — see [Credentials](#credentials). |
| `credential_header` | http, sse | Header the secret is sent under (default `Authorization`). |
| `header_credentials` | http, sse | Array of `{ header, credential }` for a server needing **more than one** secret header — see [Multiple secret headers](#multiple-secret-headers). |
| `proxy` | all | Per-server HTTP CONNECT proxy — see [Per-server proxy](#per-server-proxy). |

## How the tools appear

Each external tool is exposed to the agent as `mcp_<server>_<tool>` — e.g. a
`search_issues` tool on a server named `github` becomes `mcp_github_search_issues`.
The name is sanitised to the tool-name grammar and namespaced so it never
collides with a runnable or a built-in. The tool's own JSON-Schema input is
passed through unchanged, and its description carries a `(via MCP server '…')`
note so the agent knows the call leaves the machine.

In `[llm] tool_mode = "search"` the external tools join the deferred long tail
(reached via `describe_tool` / `use_tool`) rather than the always-inline kernel,
so a large server doesn't bloat every turn's tool list.

## Autonomy — trust is opt-in

An external tool runs on someone else's server, so by default every call is
**confirm**-gated: the operator approves it (and can edit the arguments first),
exactly like a side-effecting runnable. Set `autonomy = "autonomous"` on a server
you trust to let its tools run without a prompt, or `autonomy = "manual"` to make
them human-only (the agent can never call them). This mirrors the console's
"safe by default, opt in to trust" posture everywhere else.

In an unattended **trigger** or **schedule** agent turn with a non-empty
`action_allow` list, external MCP tools are governed like the other action tools:
name a tool in `action_allow` to keep it, or it's dropped from that turn.

## Credentials

Rather than pasting a token into `headers` or `env`, bind a **named credential**
(Settings → Credentials) to a server with `credential = "<id>"`. The secret lives
in the OS keychain (Windows Credential Manager / macOS Keychain / Secret Service)
and is resolved only at connect time — the config file holds just the id, and the
secret never appears in the config, `mcp_status`, or the logs. How it's applied:

- **http / sse** — becomes an `Authorization` header: `Bearer <secret>` for a
  `token`/`password` credential, HTTP `Basic` for a `userpass` one. Set
  `credential_header` (e.g. `X-API-Key`) to send the raw secret under a different
  header instead. A header you set explicitly in `headers` always wins.
- **stdio** — injected as the credential's name-derived env vars (the same
  convention the runnable credential channel uses — e.g. a `token` credential
  labelled `Deploy` sets `DEPLOY_TOKEN`), so the server process reads its token
  from the environment.

Because only the id is stored, a bound credential is **seed/sync-safe**: the
credential's *metadata* (id, label, kind, username) rides Config Sync / the
config seed in `runspec_credentials.toml`, while each install supplies the actual
secret in its own keychain. In the Settings → MCP tab the server's `credential`
is a dropdown populated from `mcp_credential_options()` (id + label + kind, never
secrets).

### Multiple secret headers

The single `credential` fills one header. A server that needs **more than one**
secret header — say an Atlassian MCP server wanting a Jira PAT *and* a Confluence
PAT under two different headers — uses `header_credentials`: an array of
`{ header, credential }` bindings, each sending its named credential's raw secret
under that header, resolved from the keychain at connect time.

```toml
[[mcp.servers]]
name = "atlassian"
transport = "http"
url = "https://mcp.atlassian.example/mcp"

  [[mcp.servers.header_credentials]]
  header = "X-Jira-PAT"
  credential = "jira-pat"        # a keychain credential id

  [[mcp.servers.header_credentials]]
  header = "X-Confluence-PAT"
  credential = "confluence-pat"
```

The same keychain/seed/sync guarantees apply — only the header name and the
credential **id** are stored, never the token. A literal `headers` entry (or the
single `credential`) of the same name always wins, and a binding whose credential
can't be resolved is skipped rather than breaking the connect. In Settings → MCP,
add these under **Secret headers** (a header name + a credential dropdown per
row); the field shows for http/sse servers. http/sse only — a stdio server takes
its secrets as env vars via `credential` instead.

## Per-server proxy

Servers can live on different corporate networks, so the proxy is **per server**,
not global. Set `proxy` to an HTTP CONNECT proxy URL (`http://proxy:8080`; the
scheme is optional) — it mirrors the console's `[ssh] proxy` and Config Sync's
`proxy`. For an `http`/`sse` server the console reaches the endpoint through that
proxy; for a `stdio` server it's exported to the subprocess as `HTTPS_PROXY` /
`HTTP_PROXY` / `ALL_PROXY` (so a local server that itself calls out uses it too).
Leave it unset to use the machine's default egress.

## Config seed & sync

`[mcp]` lives in `config.toml`, which is one of the files carried by **Config
Sync** and the **config seed** — so a white-label / air-gapped install can ship a
baseline set of MCP servers, and a team can distribute them from one versioned
source. Bound credentials come along too: `runspec_credentials.toml` (metadata
only) is also a synced/seeded file, and secrets stay in each machine's keychain.
See [Team config — sync & rooms](console-team.md) and the config-seed walkthrough
for how the layers combine (seed < git sync < local edits, with a `preserve` list
for machine-local keys).

## Checking status

The agent's per-turn console context lists the connected servers and their tool
counts, so it knows the tools exist. Programmatically, the bridge exposes
`mcp_status()` (availability + a per-server summary of connected state, tool
count, and any connect error) and `mcp_reload()` (reconnect after editing
`[mcp]`). A server that is slow or down at launch simply lands disconnected and
shows in the status — it never blocks the console from starting.

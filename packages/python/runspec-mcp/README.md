# runspec-mcp

Connect **Claude Code** to the runspec runnables installed across your machines —
local venvs and remote SSH venvs — from **one config file**, with credentials
injected as environment variables and each runnable's declared `autonomy` carried
into Claude Code's permission layer.

`runspec-mcp` is a small local **MCP gateway**: Claude Code spawns it as a
single MCP server; it reads your hosts + credential metadata, discovers the
runnables in every configured venv (`runspec local --format json`, locally and
over SSH), and presents them all as MCP tools. Discovery is **live** — as hosts
connect/disconnect or their runnables change, the tool list updates automatically
(no restart, no manual refresh).

Install the **Claude Code plugin** (the gateway plus the autonomy hook) from a
Claude Code session. With [uv](https://docs.astral.sh/uv/), nothing else to install:

```
/plugin marketplace add https://runspec.app/marketplace.json
/plugin install runspec-mcp@runspec
```

Without uv (or from a private index), `pip install "runspec-mcp[credentials]"`
first, then:

```
/plugin marketplace add https://runspec.app/marketplace-pip.json
/plugin install runspec-mcp@runspec-pip
```

Either way, the plugin asks for your runspec-console config folder (or a
`runspec_mcp.toml`) when you install it. See
[runspec.app/mcp](https://runspec.app/mcp/#install-the-claude-code-plugin).

**Any MCP client** (Claude Desktop, Cursor, Cline, or Claude Code without the
plugin) works too — `runspec-mcp serve` is a standard MCP stdio server. Add a
`mcpServers` entry:

```json
{ "mcpServers": { "runspec-mcp": {
    "command": "runspec-mcp", "args": ["serve"],
    "env": { "RUNSPEC_MCP_CONFIG": "/absolute/path/to/runspec_mcp.toml" } } } }
```

The Claude Code plugin adds one thing over a bare client: the PreToolUse hook that
auto-approves `autonomous` runnables. Either way, `manual` runnables are refused
by the gateway and `password` args never reach a tool schema. See
[`docs/mcp.md`](../../../docs/mcp.md) for per-client details and the config-seed
("fleet adapter") how-to.

## Config (`runspec_mcp.toml`)

```toml
[local]
venv_globs = ["~/venvs/*"]                       # local multi-venv discovery

[gateway]
refresh_interval = 30                             # background rediscovery cadence (s); 0 = off

[[host]]
name = "prod"
ssh = "deploy@prod.example.com"                  # omit ssh → this machine
runspec_paths = ["/opt/venvs/fleet/bin/runspec"]
jump = "bastion"                                 # optional ProxyJump (system ssh)

[[credential]]
id = "Windows"                                   # label → WINDOWS_* env vars
kind = "userpass"                                # userpass | password | token | ssh-key
username = "svc-bot"
# secret stored in the OS keychain:  runspec-mcp cred set Windows
# scope_hosts / scope_runnables optional — else auto-matched by a runnable's arg `env`
```

A runnable that declares it reads `WINDOWS_USERNAME` / `WINDOWS_PASSWORD` (via an
arg `env`) auto-pulls the `Windows` credential — no scope config needed.

Secrets live in the OS keychain (`keyring`), keyed by credential `id` under the
`runspec-console` service, so they are **shared with the desktop console**. Point
the gateway at an existing console setup with `--config-dir <dir>`.

## SSH keys

**No SSH key ever goes in the MCP config** — only path references. The gateway
manages its **own ed25519 keypair** and uses it as the default SSH identity, so you
never touch `~/.ssh`; authorise it on your hosts once:

```bash
runspec-mcp key                    # print the managed public key
runspec-mcp copy-key --all         # install it on every configured host (one password prompt)
```

A per-host `identity_file` path still overrides it, and ssh-agent / `~/.ssh/id_*`
remain a fallback.

## Commands

| command | what it does |
|---|---|
| `runspec-mcp serve` | the MCP gateway (what the plugin runs) |
| `runspec-mcp hosts` | list configured hosts + probe connectivity |
| `runspec-mcp discover [--host H]` | print discovered tools |
| `runspec-mcp key [--regenerate]` | show / rotate the managed SSH key |
| `runspec-mcp copy-key [--all] [HOST…]` | install the managed key on host(s) |
| `runspec-mcp cred set\|list\|rm ID` | manage credential secrets in the keychain |
| `runspec-mcp seed-status` | show bundled config-seed contributors |
| `runspec-mcp plugin-path` | print the bundled Claude Code plugin source dir |
| `runspec-mcp claude-plugin` | render the plugin for this install, print its path |
| `runspec-mcp check` | validate config + connectivity + manifest |

## Autonomy

Each runnable's `autonomy` maps onto a Claude Code permission decision via a
PreToolUse hook the plugin ships (`autonomous → allow`, `confirm`/`supervised →
ask`, `manual → deny`, unknown → ask). The gateway rewrites the hook's manifest on
every discovery change, so it is always fresh. The remote side stays authoritative
(venv denylist, `enforce_run_as`, `password` args omitted from schemas).

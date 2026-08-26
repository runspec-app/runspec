# runspec-fleet

Connect **Claude Code** to the runspec runnables installed across your machines —
local venvs and remote SSH venvs — from **one config file**, with credentials
injected as environment variables and each runnable's declared `autonomy` carried
into Claude Code's permission layer.

`runspec-fleet` is a small local **MCP gateway**: Claude Code spawns it as a
single MCP server; it reads your hosts + credential metadata, discovers the
runnables in every configured venv (`runspec local --format json`, locally and
over SSH), and presents them all as MCP tools. Discovery is **live** — as hosts
connect/disconnect or their runnables change, the tool list updates automatically
(no restart, no manual refresh).

```
pip install "runspec-fleet[credentials]"
# write runspec_fleet.toml (or point at an existing runspec-console config dir)
claude --plugin-dir "$(runspec-fleet plugin-path)"
```

## Config (`runspec_fleet.toml`)

```toml
[local]
venv_globs = ["~/venvs/*"]                       # local multi-venv discovery

[fleet]
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
# secret stored in the OS keychain:  runspec-fleet cred set Windows
# scope_hosts / scope_runnables optional — else auto-matched by a runnable's arg `env`
```

A runnable that declares it reads `WINDOWS_USERNAME` / `WINDOWS_PASSWORD` (via an
arg `env`) auto-pulls the `Windows` credential — no scope config needed.

Secrets live in the OS keychain (`keyring`), keyed by credential `id` under the
`runspec-console` service, so they are **shared with the desktop console**. Point
the gateway at an existing console setup with `--config-dir <dir>`.

## Commands

| command | what it does |
|---|---|
| `runspec-fleet serve` | the MCP gateway (what the plugin runs) |
| `runspec-fleet hosts` | list configured hosts + probe connectivity |
| `runspec-fleet discover [--host H]` | print discovered tools |
| `runspec-fleet cred set\|list\|rm ID` | manage credential secrets in the keychain |
| `runspec-fleet seed-status` | show bundled config-seed contributors |
| `runspec-fleet plugin-path` | print the bundled Claude Code plugin dir |
| `runspec-fleet check` | validate config + connectivity + manifest |

## Autonomy

Each runnable's `autonomy` maps onto a Claude Code permission decision via a
PreToolUse hook the plugin ships (`autonomous → allow`, `confirm`/`supervised →
ask`, `manual → deny`, unknown → ask). The gateway rewrites the hook's manifest on
every discovery change, so it is always fresh. The remote side stays authoritative
(venv denylist, `enforce_run_as`, `password` args omitted from schemas).

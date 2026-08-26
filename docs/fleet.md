# runspec-fleet — Claude Code across your fleet

`runspec-fleet` connects **Claude Code** to the runspec runnables installed across
your machines — local venvs and remote SSH venvs — from **one config file**. It is
a small local **MCP gateway**: Claude Code spawns it as a single MCP server; it
discovers the runnables in every configured venv and presents them all as MCP
tools, with each runnable's declared `autonomy` carried into Claude Code's
permission layer and credentials injected as environment variables.

It is the productised successor to the hand-rolled example at
`examples/claude-code-plugin/` — one plugin for the whole fleet instead of one
copy per host, live discovery instead of a hand-run manifest generator, and real
per-call credential injection.

---

## The model

```
Claude Code ──stdio MCP──▶ runspec-fleet serve   (one local process)
     ▲  tools/list_changed   │  reads runspec_fleet.toml (hosts + credential metadata)
     └──── (auto re-fetch) ───┤  OS keychain ── secrets
                              ├── local venv:  runspec local --json ; exec the runnable
                              └── remote venv: ssh host runspec local --json ; ssh exec
   PreToolUse hook ◀── autonomy.json (rewritten by the gateway on every change)
```

Discovery uses `runspec local --format json` in each venv, so the tools are
exactly what `runspec serve` on that host would expose (subcommands flattened to
leaf tools, `x-autonomy` carried, `password` args omitted). Execution mirrors
runspec-console: a discrete SSH exec of the runnable's own venv binary with the
credential environment injected per call.

---

## Install & connect

```bash
pip install "runspec-fleet[credentials]"
# write runspec_fleet.toml, then:
claude --plugin-dir "$(runspec-fleet plugin-path)"
```

`/mcp` lists the `fleet` server; tools appear as
`mcp__plugin_runspec-fleet_fleet__<name>` (host-prefixed when more than one host
is configured).

---

## Configuration

Point the gateway at a config three ways (first that applies wins):

1. A single `runspec_fleet.toml` — `--config`, `$RUNSPEC_FLEET_CONFIG`, or
   `./runspec_fleet.toml`.
2. An existing runspec-console config directory — `--config-dir <dir>` reuses that
   console's `runspec_hosts.toml` + `runspec_credentials.toml` + `config.toml`.
3. A bundled **config seed** package (see below).

```toml
[local]
venv_globs = ["~/venvs/*"]                       # local multi-venv discovery

[fleet]
refresh_interval = 30                             # background rediscovery cadence (s); 0 = off

[[host]]
name = "prod"
ssh = "deploy@prod.example.com"                  # omit ssh → this machine
runspec_paths = ["/opt/venvs/fleet/bin/runspec"] # remote venv runspec binaries
jump = "bastion"                                 # optional ProxyJump (system ssh)

[[credential]]
id = "Windows"                                   # label → WINDOWS_* env vars
kind = "userpass"                                # userpass | password | token | ssh-key
username = "svc-bot"
# scope_hosts / scope_runnables optional — else auto-matched by a runnable's arg env
```

SSH connection details (user, port, key, ProxyJump) come from `~/.ssh/config` and
the `[ssh]` section, exactly as in runspec-console; `jump` selects the system-ssh
transport for legacy hosts.

---

## Credentials

Secrets live in the **OS keychain** (`keyring`), keyed by credential `id` under
the `runspec-console` service — so they are **shared with the desktop console**.

```bash
runspec-fleet cred set Windows      # prompts, stores in the keychain
runspec-fleet cred list             # shows which env vars each credential emits
```

A credential is injected as name-derived environment variables — label `Windows`
+ kind `userpass` → `WINDOWS_USERNAME`/`WINDOWS_USER`/`WINDOWS_PASSWORD`; `Deploy`
+ `token` → `DEPLOY_TOKEN`/`DEPLOY_PAT`. Two matchers decide which credentials a
call receives:

- **Auto by arg `env`** — a runnable that declares it reads `WINDOWS_USERNAME`
  auto-pulls the `Windows` credential. Zero config: the runnable's own interface
  says what it wants.
- **Scope globs** — `scope_hosts` / `scope_runnables` cover runnables that read
  bare environment variables the MCP schema can't advertise.

---

## Autonomy

Each runnable's `autonomy` maps onto a Claude Code permission decision via a
PreToolUse hook the plugin ships:

| runspec autonomy | Claude Code | effect |
|---|---|---|
| `autonomous` | allow | runs without prompting |
| `confirm` / `supervised` | ask | the operator approves each call |
| `manual` | deny | the agent may not run it — the gateway also refuses it |
| *(unknown / no manifest)* | ask | fail closed |

The gateway rewrites the hook's manifest on **every discovery change**, so it is
always fresh — there is no manual generation step. The remote side stays
authoritative regardless of the client: the venv denylist and `enforce_run_as` at
`parse()` time, and `password` args omitted from agent-facing schemas.

---

## Config seed (corporate baseline)

A deployment package can ship a `config_seed/` directory of TOMLs and declare an
entry point so a fresh install is pre-wired with hosts + credential *metadata*
(secrets are never seeded):

```toml
[project.entry-points."runspec_fleet.config_seed"]
mycorp = "mycorp_fleet"
```

The fleet also reads the console's `runspec_console.config_seed` group, so one
seed package configures both the desktop console and the plugin. `runspec-fleet
seed-status` shows what is bundled.

---

## Commands

| command | what it does |
|---|---|
| `runspec-fleet serve` | the MCP gateway (what the plugin runs) |
| `runspec-fleet hosts` | list configured hosts + probe connectivity |
| `runspec-fleet discover [--host H]` | print discovered tools |
| `runspec-fleet cred set\|list\|rm ID` | manage credential secrets in the keychain |
| `runspec-fleet seed-status` | show bundled config-seed contributors |
| `runspec-fleet plugin-path` | print the bundled Claude Code plugin directory |
| `runspec-fleet check` | validate config + connectivity + manifest |

Inside a session the plugin also offers `/runspec-hosts` and `/runspec-refresh`
(a manual force-refresh; discovery is otherwise automatic).

---

## See also

- [Jump hosts](jump-hosts.md) — the SSH+MCP transport the fleet reuses.
- [Agent Integration](agents.md) — autonomy and the MCP surface.
- `examples/claude-code-plugin/` — the minimal, zero-dependency single-host
  reference this package supersedes for the multi-host case.

# runspec-mcp — Claude Code across your fleet

`runspec-mcp` connects **Claude Code** to the runspec runnables installed across
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
Claude Code ──stdio MCP──▶ runspec-mcp serve   (one local process)
     ▲  tools/list_changed   │  reads runspec_mcp.toml (hosts + credential metadata)
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
pip install "runspec-mcp[credentials]"
# write runspec_mcp.toml (see Configuration below), then check it:
runspec-mcp check
# and print the exact client config for your MCP client:
runspec-mcp setup claude      # Claude Code: prints `claude mcp add …` (or --apply to run it)
runspec-mcp setup vscode      # VS Code / GitHub Copilot: prints the .vscode/mcp.json entry
runspec-mcp setup json        # any other client: a plain mcpServers block
```

**Always configure the client with the absolute path `setup` prints.** An MCP
client starts the gateway itself, with its own `PATH` and working directory. If
you installed runspec-mcp in a venv, a bare `runspec-mcp` command isn't found,
and the client only reports that the server failed to connect.
`runspec-mcp check` warns when it isn't on your `PATH`.

`runspec-mcp setup claude` also prints the plugin form, which adds the autonomy
hook:

```bash
RUNSPEC_MCP_BIN=/abs/path/runspec-mcp RUNSPEC_MCP_CONFIG=/abs/path/runspec_mcp.toml \
  claude --plugin-dir "$(runspec-mcp plugin-path)"
```

`/mcp` lists the `fleet` server; tools appear as
`mcp__plugin_runspec-mcp_fleet__<name>` (host-prefixed when more than one host
is configured).

---

## Connect from any MCP client

The Claude Code plugin is just *packaging*. Underneath, `runspec-mcp serve` is a
standard **MCP stdio server**, so any client that can spawn one works — Claude
Desktop, Cursor, Cline, or Claude Code without the plugin. Add a plain
`mcpServers` entry:

```json
{
  "mcpServers": {
    "runspec-mcp": {
      "command": "runspec-mcp",
      "args": ["serve"],
      "env": { "RUNSPEC_MCP_CONFIG": "/absolute/path/to/runspec_mcp.toml" }
    }
  }
}
```

Use absolute paths for both `command` and `RUNSPEC_MCP_CONFIG`
(`runspec-mcp setup json` prints them). An MCP client's `PATH` and working
directory are unpredictable, and the gateway otherwise looks for `./runspec_mcp.toml` then its
app-dir default. Drop the block into the client's MCP config (Claude Desktop's
`claude_desktop_config.json`, Cursor/Cline's MCP settings, or a project
`.mcp.json`). For Claude Code without the plugin:

```bash
claude mcp add runspec-mcp --env RUNSPEC_MCP_CONFIG=/path/to/runspec_mcp.toml -- runspec-mcp serve
```

**What the plugin adds over a bare client — autonomy.** The PreToolUse hook that
maps `autonomy` to allow/ask/deny ships **with the Claude Code plugin only**.
Through a bare client, `autonomous` runnables are *not* auto-approved — the
client's own approve/deny prompt applies (more prompting, not less safety).
The authoritative controls hold regardless of client: **`manual` runnables are
refused by the gateway itself**, and `password` args never appear in a tool
schema. So a bare client is fully functional; the plugin just makes `autonomous`
runnables frictionless.

---

## Configuration

Point the gateway at a config three ways (first that applies wins):

1. A single `runspec_mcp.toml` — `--config`, `$RUNSPEC_MCP_CONFIG`, or
   `./runspec_mcp.toml`.
2. An existing runspec-console config directory — `--config-dir <dir>` reuses that
   console's `runspec_hosts.toml` + `runspec_credentials.toml` + `config.toml`.
3. A bundled **config seed** package (see below).

```toml
[local]
venv_globs = ["~/venvs/*"]                       # local multi-venv discovery

[gateway]
refresh_interval = 30                             # background rediscovery cadence (s); 0 = off

[[host]]
name = "prod"
ssh = "deploy@prod.example.com"                  # omit ssh → this machine
runspec_paths = ["/opt/venvs/fleet/bin/runspec"] # remote venv runspec binaries
jump = "bastion"                                 # optional ProxyJump (system ssh)

[[credential]]
id = "Windows"                                   # label defaults to the id → WINDOWS_* env vars
kind = "userpass"                                # userpass | password | token | ssh-key
username = "svc-bot"
# scope_hosts / scope_runnables optional — else auto-matched by a runnable's arg env
```

SSH connection details (user, port, ProxyJump) come from `~/.ssh/config` and the
`[ssh]` section, exactly as in runspec-console; `jump` selects the system-ssh
transport for legacy hosts.

---

## SSH & keys

**No SSH key ever lives in the MCP config.** The MCP server entry (`plugin.json`)
and `runspec_mcp.toml` carry only *paths* and credential *ids* — never key
material; credential secrets live in the OS keychain.

To save you from setting anything up in `~/.ssh`, the gateway **manages its own
ed25519 keypair** (in its app dir) and uses it as the default SSH identity for
every host. You authorise it on your hosts once, per-user — no shared service
account:

```bash
runspec-mcp key                    # print the gateway's managed public key
runspec-mcp copy-key --all         # install it on every configured host (one password prompt)
runspec-mcp copy-key prod db-1     # …or just the named hosts
```

Precedence and fallbacks are unchanged: a per-host `identity_file` (a path) still
overrides the managed key, and the ssh-agent / default `~/.ssh/id_*` keys remain a
fallback — so an existing SSH setup keeps working. `runspec-mcp key --regenerate`
rotates the managed keypair (re-run `copy-key` afterwards).

---

## Credentials

Secrets live in the **OS keychain** (`keyring`), keyed by credential `id` under
the `runspec-console` service — so they are **shared with the desktop console**.

```bash
runspec-mcp cred set Windows      # prompts, stores in the keychain
runspec-mcp cred list             # shows which env vars each credential emits
```

**On Linux, the keychain is the desktop's Secret Service** (GNOME Keyring or
KWallet), running and unlocked in your session. Over SSH or on a headless server
there usually isn't one, and `cred set` says so. Either store secrets from a
desktop session, or install a file-based keyring backend (for example
`keyrings.cryptfile`, which encrypts with a password) and select it with
`PYTHON_KEYRING_BACKEND`.

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
always fresh — there is no manual generation step. It lives in your config
directory (`~/.config/runspec-mcp/autonomy.json`, or `%APPDATA%\runspec-mcp\` on
Windows; `$RUNSPEC_PLUGIN_MANIFEST` overrides it), where both the gateway and the
hook find it. The remote side stays
authoritative regardless of the client: the venv denylist and `enforce_run_as` at
`parse()` time, and `password` args omitted from agent-facing schemas.

---

## Config seed — a "fleet adapter" package

For a team or air-gapped deployment, you don't want every user hand-writing a
`runspec_mcp.toml`. Ship a small pip package — a **config-seed adapter** — that
bundles the baseline **hosts + credential metadata** as package data, so
`pip install mycorp-fleet` leaves the gateway pre-wired. (Secrets are never in
the package — see below.)

**1. Lay out the package** with a `config_seed/` data directory:

```
mycorp_fleet/
  __init__.py
  config_seed/
    runspec_hosts.toml          # the fleet's hosts
    runspec_credentials.toml    # credential metadata (no secrets)
    config.toml                 # optional: [ssh] / [local] / [gateway] defaults
```

**2. Declare the entry point** in the package's `pyproject.toml`:

```toml
[project.entry-points."runspec_mcp.config_seed"]
mycorp = "mycorp_fleet"          # the module holding the config_seed/ dir
```

**3. Seed hosts** (`config_seed/runspec_hosts.toml`) — same schema as a hand-written
config:

```toml
[[host]]
name = "prod"
ssh = "deploy@prod.example.com"
runspec_paths = ["/opt/venvs/fleet/bin/runspec"]

[[host]]
name = "db-1"
ssh = "svc@db-1"
jump = "bastion"
runspec_paths = ["/opt/venvs/ops/bin/runspec"]
```

**4. Seed credential *metadata*** (`config_seed/runspec_credentials.toml`) — ids,
labels, kinds, usernames, scopes. **Never secret values:**

```toml
[[credential]]
id = "Windows"
label = "Windows"
kind = "userpass"
username = "svc-bot"
scope_hosts = ["prod", "db-1"]
```

**5. Install and verify.** After `pip install mycorp-fleet`, the gateway folds the
seed in automatically on start; `runspec-mcp seed-status` lists the contributing
package and version, and `runspec-mcp hosts` shows the seeded fleet.

```bash
runspec-mcp seed-status     # mycorp-fleet 1.0.0 → runspec_hosts.toml, runspec_credentials.toml
```

**How the fold works.** Seeds are the **lowest** config layer: they're merged
**by identity** (host `name`, credential `id`) *under* any local `runspec_mcp.toml`
edits and a live config-sync — so a package upgrade rolls the baseline forward
without clobbering an operator's own hosts. The gateway also reads the console's
`runspec_console.config_seed` group, so **one corporate package can seed both the
desktop console and the MCP gateway**.

**Secrets stay per-user.** The package carries only metadata; each operator
supplies their own secret once into the OS keychain — no shared service account:

```bash
runspec-mcp cred set Windows
```

Further reading: [`docs/white-label.md`](white-label.md) walks the same mechanism
end-to-end on the console side (branding + `preserve` + re-assert-on-upgrade); the
`docs/design/config-seed.md` design note has the merge internals.

---

## Commands

| command | what it does |
|---|---|
| `runspec-mcp serve` | the MCP gateway (what the plugin runs) |
| `runspec-mcp hosts` | list configured hosts + probe connectivity |
| `runspec-mcp discover [--host H]` | print discovered tools |
| `runspec-mcp key [--regenerate]` | show / rotate the gateway's managed SSH key |
| `runspec-mcp copy-key [--all] [HOST…]` | install the managed public key on host(s) |
| `runspec-mcp cred set\|list\|rm ID` | manage credential secrets in the keychain |
| `runspec-mcp seed-status` | show bundled config-seed contributors |
| `runspec-mcp plugin-path` | print the bundled Claude Code plugin directory |
| `runspec-mcp check` | validate config + connectivity + manifest |

Inside a session the plugin also offers `/runspec-hosts` and `/runspec-refresh`
(a manual force-refresh; discovery is otherwise automatic).

---

## See also

- [Jump hosts](jump-hosts.md) — the SSH+MCP transport the fleet reuses.
- [Agent Integration](agents.md) — autonomy and the MCP surface.
- `examples/claude-code-plugin/` — the minimal, zero-dependency single-host
  reference this package supersedes for the multi-host case.

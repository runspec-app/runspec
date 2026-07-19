# runspec-fleet — a Claude Code plugin for remote runnables

Expose the runnables installed in a **remote venv** as MCP tools inside
**Claude Code**, with each runnable's declared `autonomy` carried over as a
Claude Code permission decision.

It works exactly like `runspec jump`: Claude Code spawns
`ssh <host> runspec serve` and speaks MCP over the pipe. No ports, no daemon —
auth is your SSH config.

```
examples/claude-code-plugin/
├── .claude-plugin/
│   ├── plugin.json          # MCP server: ssh <host> runspec serve
│   └── marketplace.json     # lets this dir act as a local marketplace
├── hooks/
│   ├── hooks.json           # PreToolUse matcher for this plugin's tools
│   └── autonomy_gate.py     # autonomy → allow / ask / deny
├── scripts/
│   └── generate_manifest.py # bakes autonomy.json from the remote serve
├── autonomy.example.json    # what a baked manifest looks like
└── README.md
```

## Quick start

**0. Prerequisites.** The remote host has a venv (with a meaningful name — see
`runspec serve`'s venv-name rule) containing `runspec` plus your runnable
packages, and you can `ssh <host>` non-interactively (key auth; `BatchMode=yes`
is forced, so password prompts will fail by design). `python3` on the local
machine for the hook.

**1. Copy this directory** somewhere outside the repo (one copy per host):

```bash
cp -r examples/claude-code-plugin ~/claude-plugins/runspec-myhost
cd ~/claude-plugins/runspec-myhost
```

**2. Point it at your host.** Either export the variables the plugin expands:

```bash
export RUNSPEC_PLUGIN_HOST=user@myhost
export RUNSPEC_PLUGIN_BIN=/opt/venvs/fleet/bin/runspec   # optional, default: runspec
```

or edit `.claude-plugin/plugin.json` and replace `${RUNSPEC_PLUGIN_HOST}` /
`${RUNSPEC_PLUGIN_BIN:-runspec}` with literal values (recommended when you run
several host copies side by side — also give each copy a distinct `name`, and
mirror it in the `matcher` in `hooks/hooks.json`).

**3. Bake the autonomy manifest** (stdlib only, no local runspec needed):

```bash
python3 scripts/generate_manifest.py --host user@myhost --bin /opt/venvs/fleet/bin/runspec
# ✓  12 tool(s) from 'fleet' → ./autonomy.json  (7 autonomous, 4 confirm, 1 manual)
```

Re-run this whenever the remote venv's runnables change. A stale manifest is
safe — unknown tools fall back to "ask" — just noisier.

**4. Load the plugin.** For a quick test:

```bash
claude --plugin-dir ~/claude-plugins/runspec-myhost
```

To install persistently (this directory doubles as a single-plugin
marketplace):

```bash
claude plugin marketplace add ~/claude-plugins/runspec-myhost
claude plugin install runspec-fleet@runspec-examples
```

(or `/plugin marketplace add …` + `/plugin install …` inside a session, then
`/reload-plugins`).

**5. Use it.** `/mcp` lists the server; tools appear as
`mcp__plugin_runspec-fleet_fleet__<runnable>` (one per runnable, one per leaf
subcommand — same flattening as `runspec serve` everywhere else).

## How autonomy maps

The PreToolUse hook (`hooks/autonomy_gate.py`) reads `autonomy.json` and
returns a permission decision for every call to this plugin's tools:

| runspec autonomy | Claude Code decision | effect |
|---|---|---|
| `autonomous` | `allow` | runs without prompting |
| `confirm` | `ask` | user approves each call |
| `supervised` | `ask` | user approves each call |
| `manual` | `deny` | the agent cannot run it |
| *(not in manifest / manifest missing / hook error)* | `ask` | fail closed |

Per-arg escalation is applied when the manifest carries per-arg levels: the
most restrictive level among the runnable and the args actually provided wins,
mirroring runspec's `effective_autonomy()`. (Requires runspec ≥ 0.50.0 on the
remote — older serves don't emit per-arg autonomy in tool schemas, so those
manifests fall back to the tool-level default.)

Notes:

- A hook `allow` bypasses your own permission prompts for that call — the
  manifest is trusted config. Keep it generated, don't hand-edit it.
- The hook is a client-side convenience, not the security boundary. The remote
  side still enforces the venv denylist at `parse()` time, and `password`-typed
  args are never in the tool schema at all.
- `x-autonomy-reason` from the runnable surfaces in the permission prompt.

## Local venvs

The same plugin works for runnables installed on **this machine** — swap the
SSH command for the venv's own binary in `.claude-plugin/plugin.json`:

```json
"mcpServers": {
  "fleet": {
    "type": "stdio",
    "command": "/opt/venvs/fleet/bin/runspec",
    "args": ["serve"]
  }
}
```

and bake the manifest without `--host`:

```bash
python3 scripts/generate_manifest.py --bin /opt/venvs/fleet/bin/runspec
```

Everything else — discovery, autonomy gating, the venv-name rule — is
identical. Note the serve *context* differs: spawned locally there's no
`SSH_CONNECTION`, so runnables restricted with `serve = ["remote"]` won't
surface (and `serve = ["local"]` ones will). Use one plugin copy per venv,
local or remote — the manifest is per-venv.

## Secrets

Preferred: **keep secrets on the remote host.** Runnables read them from the
service account's environment (profile, systemd unit, or a 600-mode env file)
via runspec's normal env resolution — the plugin then carries no secrets and
the agent never sees a value in either direction.

If a secret must come from your machine, remember that the `env` block in
`plugin.json` only reaches the **local `ssh` process**. Forward it explicitly:

```bash
# ~/.ssh/config (client)          # sshd_config (remote)
Host myhost                       AcceptEnv JSM_PAT
  SendEnv JSM_PAT
```

and set `"env": {"JSM_PAT": "${JSM_PAT}"}` on the server entry so the value is
pulled from your environment (or an OS keychain via plugin `userConfig` with
`sensitive: true`) — never a literal in the committed file. Don't inline
secrets into the remote command string; that lands them in the remote's
process list.

## Troubleshooting

- **Server won't start** — run the exact command yourself:
  `ssh -o BatchMode=yes <host> <bin> serve` then paste the initialize line from
  `runspec serve`'s own hint. Common causes: password-only auth (BatchMode
  refuses), generic venv name, no runspec-aware packages installed.
- **Every call prompts** — the manifest is missing or stale; re-run
  `scripts/generate_manifest.py` (the hook's reason text says which).
- **Hook errors** — run it directly:
  `echo '{"tool_name":"mcp__plugin_runspec-fleet_fleet__greeter","tool_input":{}}' | python3 hooks/autonomy_gate.py`

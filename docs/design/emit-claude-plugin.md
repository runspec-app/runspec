# Design: `runspec emit --claude-plugin`

> **Status: superseded for the multi-host case by the `runspec-mcp` package**
> (`packages/python/runspec-mcp/`, `docs/mcp.md`). Rather than *emitting* a
> per-host plugin directory, `runspec-mcp` ships a single installable plugin
> backed by a local MCP **gateway**: one config file lists all hosts (local +
> remote SSH venvs), discovery is **live** (the gateway declares
> `tools.listChanged` and pushes `notifications/tools/list_changed` from a
> background thread, and rewrites the autonomy manifest on every change — no
> manual `generate_manifest.py`), and credentials are injected per call as
> name-derived env vars (keychain-backed, shared with runspec-console),
> auto-matched to a runnable by its declared arg `env`. The hand-rolled
> `examples/claude-code-plugin/` remains as the minimal, zero-dependency
> single-host reference.
>
> The original prototype notes below are retained for the rationale (why the
> PreToolUse hook carries autonomy, the baked-manifest-vs-live-query trade-off,
> the secrets layering) — all of which `runspec-mcp` builds on.

## Problem

Claude Code can consume any MCP stdio server, and `runspec serve` is one. The
transport story is already solved — it's the `runspec jump` pattern with Claude
Code as the client:

```
Claude Code ──spawns──▶ ssh -o BatchMode=yes <host> <venv>/bin/runspec serve
                        └── MCP JSON-RPC over the SSH pipe ──┘
```

What a bare `.mcp.json` entry *loses* is runspec's governance model:

1. **Autonomy.** Claude Code's permission layer knows nothing about
   `autonomy = "autonomous" / "confirm" / "supervised" / "manual"`. Every MCP
   tool gets the same default treatment (ask, until the user allowlists it) —
   `autonomous` runnables nag, and nothing stops an agent from *requesting* a
   `manual` runnable.
2. **Distribution.** A team wants "install this, get the fleet" — which is
   exactly what a Claude Code plugin is for (a plugin bundles `mcpServers` and
   hooks, and starts them when enabled).

## What the prototype does

Claude Code plugins can ship a **PreToolUse hook** that matches the plugin's
own MCP tools and returns a per-call `permissionDecision`. That is a
strictly better fit than static permission rules (which plugins cannot ship
anyway — only user/project `settings.json` can): a hook sees the **call**, so
it can implement runspec's escalation rule, not just a per-tool constant.

The mapping:

| runspec autonomy | hook decision | rationale |
|---|---|---|
| `autonomous` | `allow` | declared safe for agents; no prompt |
| `confirm` | `ask` | the operator approves the call (Claude Code's native gate) |
| `supervised` | `ask` | Claude Code has no richer interactive level; `ask` with the reason string is the closest fit |
| `manual` | `deny` | operator-only; the agent may not even request it |
| unknown / no manifest / hook error | `ask` | fail closed, never `allow` |

Where does the hook get each tool's autonomy? **`runspec serve` already
advertises it**: `_build_schema(…, "mcp")` emits `x-autonomy` (and
`x-autonomy-reason`) on every tool schema, so a plain MCP `tools/list` carries
everything needed. The prototype's `generate_manifest.py` runs
initialize + tools/list against the serve (over SSH or locally) and bakes the
result into a committed `autonomy.json`; the hook reads that file per call.

Baked-manifest-vs-live-query is deliberate: a hook runs on every tool call and
must be fast and offline; a per-call SSH round-trip is neither. The manifest is
the same *committed-artifact-with-drift-tolerance* model as `runspec stubs` —
regenerate on change; drift degrades to "ask", never to "allow" (new tools are
absent from the manifest, and the deny/allow judgment is re-derived from the
manifest only, so a stale manifest can only be wrong about tools that no
longer exist).

Two enforcement notes:

- A hook `allow` bypasses the user's own permission config for that call. The
  manifest is therefore trusted config — generated, reviewed, not hand-edited.
- The hook is a *convenience* layer. The authoritative controls stay
  remote-side, at `parse()` time: the venv denylist, `enforce_run_as`, and the
  omission of `password` args from agent-facing schemas all hold regardless of
  what any client does.

## Fidelity gaps (and the fixes)

**Per-arg autonomy escalation** *(closed in 0.50.0)*. runspec's effective
autonomy is per-*call*: `effective_autonomy()` takes the most restrictive
level among the runnable and every *provided* arg with its own `autonomy`.
The hook implements exactly this rule (manifest `args: {name: level}`), and
since 0.50.0 `_arg_to_json_schema()` emits arg-level `x-autonomy` into schema
properties, so generated manifests carry the per-arg levels. Against a remote
running an older runspec the manifest simply has no per-arg levels and the
hook applies the tool-level default — under-restricting only tools whose
*base* level is `autonomous` with an escalating arg (rare by construction),
with the remote side unaffected either way.

**`supervised` is flattened to `ask`.** Claude Code has allow/ask/deny; the
distinction between confirm and supervised survives only in the prompt's
reason text. Acceptable — supervised's semantics (operator watches the run)
don't exist in Claude Code's model anyway.

**Autonomy of the transport itself.** The plugin trusts SSH: whoever can run
the Claude Code session can run whatever the remote venv account can. Same
trust statement as `runspec jump`; documented, not solved here.

## Secrets

Three-layer story, unchanged from the console's philosophy:

1. **Best: secrets live on the remote host** in the service account's
   environment (profile / systemd unit / 600-mode env file), read through
   runspec's normal env resolution (`JSM_PAT`, `RUNSPEC_<RUNNABLE>_ARG_<NAME>`).
   The plugin carries nothing; the agent never sees a value — `password` args
   aren't even in the schema.
2. **If client-side:** the `mcpServers` `env` block supports `${VAR}` /
   `${VAR:-default}` expansion and plugin `userConfig` with `sensitive: true`
   (OS keychain, `${user_config.KEY}`), so no secret ever lives in the
   committed plugin file. But `env` reaches only the **local `ssh` process** —
   crossing to the remote requires `SendEnv` (client) + `AcceptEnv` (sshd).
3. **Never:** inline a secret in the remote command string — it shows in the
   remote process list.

## What `runspec emit --claude-plugin` would add

The prototype is copy-and-edit; the emitter makes it generated. Following the
`emit --rundeck` / `--ansible` pattern (file emit, user installs):

```
runspec emit --claude-plugin [--host user@myhost] [--bin PATH] [--out DIR]
```

writes a complete plugin directory — `plugin.json` (name derived from the
`[config]` name / venv name, host baked in), `hooks.json` with a matcher
scoped to that plugin name, the gate hook, and a freshly baked
`autonomy.json` — from the same discovery data `runspec local` uses (in-venv)
or a jump-style tools/list (with `--host`). A `--check` mode compares the
baked manifest against live discovery for CI freshness, mirroring
`runspec stubs --check`.

Open questions for the emitter (deliberately unresolved in the prototype):

- **One plugin per host vs one plugin, many servers.** A fleet could emit a
  single plugin whose `mcpServers` has one entry per host. Leaning per-host:
  matches how hosts come and go, and keeps tool-name prefixes short.
- **Where the emitter runs.** In-venv emit (like `stubs`) gets full spec
  access including per-arg autonomy without needing the serve schema change;
  remote emit only sees what tools/list carries. Supporting both keeps the
  serve schema the contract.
- **Windows hook portability.** The hook invokes `python3`; the emitter should
  write a platform-appropriate command line.

Not in scope, matching the "no workflow engine" cliff: no push-to-marketplace,
no auto-regeneration daemon — the manifest regenerates when a human (or CI)
runs the command, same as stubs.

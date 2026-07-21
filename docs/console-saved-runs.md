# Saved runs

A **saved run** is a deterministic preset: a runnable + target host(s) + fixed
args, with optional **open parameters** filled at run time. It is the
declarative sibling of a [runbook](console-runbooks.md) — a runbook is
instructions the *agent* follows; a saved run involves no model at all and
fires exactly as stored, every time.

Use one whenever you find yourself re-running the same runnable with the same
targeting and mostly the same args: a rolling restart of a group, a disk check
across a candidate host list, a backup with one open `reason` field.

---

## The format

Saved runs live in `runspec_saved_runs.toml` as a `[[saved_run]]` collection:

```toml
[[saved_run]]
id             = "restart-web"
title          = "Restart web tier"
description    = "Rolling restart of nginx across production"
runnable       = "service-restart"
group          = "Production"       # exactly one of host/group/hosts/selected_hosts
credential_ids = []                 # console credential ids — never secrets

[saved_run.args]                    # fixed args, baked in
service = "nginx"

[saved_run.params]                  # open params — the runspec arg grammar
reason = { required = true }
```

Targeting mirrors a runnable-action schedule exactly — one of:

| Key | Behaviour |
|---|---|
| `host` | one host |
| `group` | fan out across every member of the host group that carries the runnable |
| `hosts` | candidate list — the run lands on the **first connected** one (failover) |
| `selected_hosts` | explicit list — fan out to every host in it |

`params` is a runspec **args table** — the same grammar as a runnable's args
and a runbook's `[runbook.args]` (types, choices, defaults, `required`), so a
param renders and validates exactly like a runnable arg. A param may not shadow
a fixed arg: values fill *missing* args only, deterministically. An optional
`command` (array) names a subcommand path.

The file **rides Config Sync**, merged by `id` — a team shares one set. That is
why fixed args must never contain secrets: attach a credential by id instead
(`credential_ids`; the secret stays in the OS keychain and is injected at run
time). A `password`-typed *param* is fine — it is typed at run time and rides
the secret env channel.

## Running one

- **Forms tab** — saved runs appear as their own lane. Clicking one opens a
  dialog showing what is baked in (target, fixed args, credentials) and a field
  per open param; a preset with no params runs immediately. Multi-host presets
  render one Console run block per host.
- **Agent** — the chat agent sees your saved runs and fires them with its
  `run_saved_run` tool. The autonomy gate reflects the **underlying runnable**:
  an `autonomous` runnable's preset runs without a prompt, a `confirm` one
  parks at a single grouped, editable confirm covering every target host
  (most-restrictive wins; a `manual` runnable blocks the preset entirely). The
  agent can also manage them via `manage_saved_runs` (chat-only, mutations
  confirm-gated).
- **Schedules** — a schedule with `action = "saved_run"` fires a preset on a
  cadence, its `values` table pre-filling the open params. The schedule carries
  no host: targeting always lives on the preset. *Run now* works as for any
  schedule.

## Editing

**Settings → Saved runs** is the editor (id, targeting, fixed args, open
params, credentials). Entries are validated on save — exactly one targeting
mode, params parsed with the core arg machinery — so a bad entry never lands in
the synced file.

## Saved run vs runbook vs schedule

| | Saved run | Runbook | Schedule (runnable) |
|---|---|---|---|
| Executes via | direct invoke, no model | agent turn (LLM follows steps) | direct invoke, no model |
| Parameterised | typed open params | typed args + `{{placeholders}}` | fixed args only |
| On demand | Forms lane, agent tool | `$` menu, agent tool | *Run now* button |
| Synced | yes | yes | no (machine-local) |

A schedule's runnable action still exists and is unchanged — a saved run is the
reusable, shareable, parameterised version of the same idea, available outside
the Schedules tab.

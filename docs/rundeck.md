# Rundeck jobs

`runspec emit --rundeck` generates [Rundeck](https://www.rundeck.com/) job
definitions from your installed runnables, so `runspec.toml` stays the single
source of truth for a Rundeck fleet instead of a hand-maintained job that drifts
whenever an arg changes.

- **One job per leaf command.** A plain runnable emits one job; a runnable with
  subcommands flattens to one job per fully-pathed leaf — `cron_add`,
  `cron_list` — exactly like `runspec serve`, each carrying its merged
  global + command args.
- **JSON output**, generated with the standard library alone (no YAML
  dependency). Targets **Rundeck 4.17.6**.
- Files land in `{git_root}/jobs/{job}.json`, safe to commit as deployment
  artefacts.

## Quick start

```bash
# From your project (the venv where the runnables are installed):
runspec emit --rundeck --venv /opt/app/.venv
# Wrote jobs/scan.json
# Wrote jobs/deploy_start.json
# ...
```

Then import into Rundeck (see below), or pipe straight to the `rd` CLI:

```bash
runspec emit --rundeck --venv /opt/app/.venv --stdout | rd jobs load -F json -p myproject
```

### Importing into Rundeck

The emitted files are Rundeck **job-definition JSON**. Import them with the API
or the `rd` CLI:

```bash
rd jobs load -f jobs/scan.json -F json -p myproject
# or the API:
curl -H "X-Rundeck-Auth-Token: $TOKEN" -H "Content-Type: application/json" \
     --data-binary @jobs/scan.json \
     "$RUNDECK_URL/api/44/project/myproject/jobs/import?format=json"
```

> The Rundeck **web UI** upload dialog historically offers only XML and YAML —
> import JSON through the API or `rd`. Each job carries a **stable UUID**
> (derived from project + group + job name), so re-emitting and re-importing
> **updates** the same job in place rather than creating a duplicate.

## What's in a job

Every job runs a single Bash **script step** — never a plain `exec`/command
step, because a static command string can't conditionally include a `--flag`
from a boolean option. The script rebuilds the CLI invocation from Rundeck's
`RD_OPTION_*` option environment variables:

- **Named args** are added only when non-empty: `--depth "$RD_OPTION_DEPTH"`.
- **Flags** are added only when `"true"`.
- **Positionals** are appended in `position` order.
- **`rest`** args are word-split after a literal `--`.
- **Secrets** never touch the command line — see below.

### Type mapping

Rundeck has no native int/float/bool/path option types, so runspec types map
onto plain text options with light validation:

| runspec type | Rundeck option |
|---|---|
| `str`, `path` | text |
| `int`, `float` | text with a validating `regex` |
| `choice` | text, `values: [...]`, `enforced: true` |
| `flag` | text, `values: ["true","false"]`, `enforced: true`, defaulting to the flag's boolean default |
| `rest` | text (space-separated; word-split after `--`) |
| `password` | **secure** option (see below) |

Required args set `required: true`; defaults are stringified into `value`. Range,
pattern, and positional/rest hints that Rundeck can't model are surfaced in the
option's description.

### Secrets

A `password` arg becomes a Rundeck **secure option** (`secure: true`,
`valueExposed: true`): the operator enters it at run time, it is encrypted, and
it is exposed to the script only as `RD_OPTION_<NAME>`. The script exports it on
the runspec secret channel —

```bash
export RUNSPEC_DEPLOY_ARG_TOKEN="${RD_OPTION_TOKEN:-}"
```

— so it reaches the runnable through the same `RUNSPEC_<RUNNABLE>_ARG_<NAME>`
env var runspec already reads passwords from, and **never appears on the command
line or in logs**.

## Nodes and load-balancing

By default a job has no node filter and runs on the Rundeck server. Give it a
node filter and it dispatches to that node set with an **orchestrator random
subset of 1** — each run lands on one random node of the set, so a scheduled or
triggered job spreads its load:

```bash
runspec emit --rundeck --venv /opt/app/.venv --node-filter 'tags: web'
```

produces, on each job:

```json
"nodefilters": { "filter": "tags: web", "dispatch": { "threadcount": 1, "keepgoing": false } },
"orchestrator": { "type": "subset", "configuration": { "count": "1" } }
```

## Schedules, notifications, and per-runnable settings

Rundeck-specific settings that aren't part of a runnable's interface live in a
`[<runnable>.meta.rundeck]` pass-through table (project-wide defaults go under
`[config.meta.rundeck]`). runspec never interprets `meta`; the emitter reads it.

```toml
[config.meta.rundeck]
venv  = "/opt/app/.venv"
group = "myteam"

[deploy]
description = "Deploy the app"
require-command = true

[deploy.meta.rundeck]
venv        = "/opt/deploy/.venv"
node_filter = "tags: deployers"
schedule    = "0 0 2 * * ?"        # Quartz crontab (6/7 fields), NOT Unix 5-field cron

[deploy.meta.rundeck.notifications.onfailure]
email = "ops@example.com"

[deploy.meta.rundeck.notifications.onsuccess]
webhook = "https://hooks.example.com/deploy"
```

### `meta.rundeck` keys

| Key | Effect |
|---|---|
| `venv` | Venv whose `bin/` holds the runnable binary (the script invocation path) |
| `group` | Rundeck job group (default `runspec`) |
| `node_filter` | Node filter; presence enables orchestrator subset=1 |
| `orchestrator` | `"subset"` (default when a filter is set) or `"none"` |
| `threadcount` | Dispatch thread count (default 1) |
| `schedule` | Quartz crontab string → the job's `schedule` |
| `notifications` | `onsuccess`/`onfailure`/`onstart` → `email` (recipients) and/or `webhook` (urls) |
| `loglevel` | Job log level (default `INFO`) |
| `strategy` / `keepgoing` | Workflow strategy (default `node-first`) / keepgoing (default `false`) |
| `timeout` / `retry` | Job timeout / retry count |

Precedence: CLI flag → `[<runnable>.meta.rundeck]` → `[config.meta.rundeck]` →
built-in default.

## Command reference

| Flag | Meaning |
|---|---|
| `--rundeck` | Select the Rundeck target (required) |
| `--venv <path>` | Venv `bin/` for the job scripts (project-wide) |
| `-d, --output-dir <dir>` | Where to write (default `{git_root}/jobs`) |
| `-r, --runnable <name>` | Emit only this runnable |
| `--group <name>` | Rundeck job group (default `runspec`) |
| `--node-filter <expr>` | Node filter → orchestrator subset=1 load-balancing |
| `--project <name>` | Rundeck project (used for stable job UUIDs) |
| `--check` | Verify emitted files are up to date; write nothing. Exit 1 if stale |
| `--stdout` | Print the combined JSON array to stdout instead of writing files |

### Keeping jobs fresh in CI

Commit `jobs/` and guard drift the same way `runspec stubs --check` does:

```bash
runspec emit --rundeck --venv /opt/app/.venv --check
# ✗  jobs is out of date — run 'runspec emit --rundeck'   (exit 1)
```

## Not covered (yet)

- **`run_as`** — the script invokes the venv binary directly; it does not wrap
  the call in `sudo`/`su`. Escalate on the Rundeck node executor if needed.
- **Group constraints** (`exclusive`, `exactly-one`, …) have no Rundeck
  equivalent; they are noted in option descriptions and still enforced by the
  runnable at runtime.
- **`multiple`** (repeatable) args emit as a single text option.

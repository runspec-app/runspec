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
  dependency). Safe on any Rundeck version, including those before 5.20 that
  don't escape option values (see
  [How values reach the runnable](#how-values-reach-the-runnable)).
- Files land in `{git_root}/jobs/{job}.json`, safe to commit as deployment
  artefacts.

## Quick start

```bash
# From your project (the venv where the runnables are installed), running each
# job's command as the venv's service account:
runspec emit --rundeck --venv /opt/app/.venv --run-as svc
# Wrote jobs/scan.json
# Wrote jobs/deploy_start.json
# ...
```

Then import into Rundeck (see below), or pipe straight to the `rd` CLI:

```bash
runspec emit --rundeck --venv /opt/app/.venv --run-as svc --stdout | rd jobs load -F json -p myproject
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

## Syncing jobs with `runspec-rundeck`

`runspec emit --rundeck` writes files; the separate
[`runspec-rundeck`](https://pypi.org/project/runspec-rundeck/) package pushes
them to a live instance over the REST API — a **JVM-free replacement for
`rd jobs load`** you can drop into a CI publish stage, with no Java on the
runner.

```bash
pip install runspec-rundeck
```

| Command | Autonomy | What it does |
|---|---|---|
| `rundeck sync` | confirm | Discover every runnable in the venv, emit a job per leaf command, and import them into a project |
| `rundeck push` | confirm | Import already-emitted `jobs/*.json` (or a whole `jobs/` directory) |
| `rundeck list` | autonomous | List a project's jobs |

`sync` is the headline verb. It runs `runspec emit --rundeck --stdout` in its own
venv (so it sees exactly the runnables installed alongside it), skips hidden
(`discoverable = false`) helpers, and imports the rest with
`dupeOption=update&uuidOption=preserve` — **create-when-missing,
update-when-present, rename-safe**, because identity is the emitted UUID.

```bash
export RUNDECK_URL=https://rundeck.example.com
export RUNDECK_TOKEN=…            # a Rundeck API token
rundeck sync --project ops
```

Because a publish stage re-emits every run, the platform's jobs stay in lockstep
with the installed runnables — arguments included. `sync` covers **every runnable
in the venv** by default; `--runnable <name>` scopes it to one.

### Pruning removed runnables

Import never deletes, so `--prune` removes jobs in the managed group whose UUID
wasn't in this push — a runnable removed from the venv has its job cleaned up:

```bash
rundeck sync --project ops --prune
```

Prune is scoped to the groups the pushed jobs declare, so it never touches jobs
the sync doesn't own.

### Environments and regions

Auth is environment-variable first, with `RUNDECK_<ENV>_*` prefixes selected by
`--env` — a free-form label, so it carries `dev`/`uat`/`prod` **and** regional
labels like `prod-eu`:

```bash
export RUNDECK_PROD_EU_URL=https://rundeck.eu.example.com
export RUNDECK_PROD_EU_TOKEN=…
rundeck sync --env prod-eu --project ops
```

Run one invocation per instance from a CI matrix. UUIDs only need to be unique
*within* an instance, so the same emitted jobs push cleanly to every separate
instance. For private-CA or proxied instances, `--ca-bundle` / `--proxy` /
`--insecure` and the `--token` password arg (the runspec-console secret channel)
override the environment.

A GitHub Actions publish stage, one job per instance:

```yaml
publish-rundeck:
  strategy:
    matrix:
      target: [uat, prod-eu, prod-us]
  steps:
    - run: pip install ./dist/*.whl runspec-rundeck
    - run: rundeck sync --env ${{ matrix.target }} --project ops --prune
      env:
        RUNDECK_UAT_URL: ${{ vars.RUNDECK_UAT_URL }}
        RUNDECK_UAT_TOKEN: ${{ secrets.RUNDECK_UAT_TOKEN }}
        RUNDECK_PROD_EU_URL: ${{ vars.RUNDECK_PROD_EU_URL }}
        RUNDECK_PROD_EU_TOKEN: ${{ secrets.RUNDECK_PROD_EU_TOKEN }}
        # …one URL/TOKEN pair per target…
```

### Corporate wrapper: bake in instances, regions, and fan-out

`runspec-rundeck-core` holds all the logic with **no runspec dependency**, so a
private package can import it, hard-code your instance topology (URLs, proxy,
projects), and expose `--region` / `--env` that resolve to a concrete instance —
plus an optional fan-out that pushes to every matching instance in one run. Only
the generic `runspec-rundeck` is published; your topology stays in your package.
This is the same split the rest of the ecosystem uses (`runspec-jsm` /
`runspec-snow` bake in a service-desk id; here you bake in a fleet).

A minimal `mycorp-rundeck` wrapper — `cli.py` resolves a baked map and fans out
with the core `sync_target`:

```python
import runspec as rs
from runspec_rundeck_core import config_from_env, sync_target
from runspec_rundeck.emit import discover_visibility, emit_jobs, filter_visible

# Your instance topology — the one thing a wrapper adds. Secrets stay in the
# environment / keychain; only URLs and projects are baked in.
INSTANCES = {
    ("eu", "prod"): {"url": "https://rundeck.eu.example.com", "project": "ops"},
    ("us", "prod"): {"url": "https://rundeck.us.example.com", "project": "ops"},
    ("eu", "uat"):  {"url": "https://rundeck-uat.eu.example.com", "project": "ops"},
}
PROXY = "http://proxy.example.com:8080"   # corporate egress, baked in


def targets(region, env):
    """One concrete pair, or every matching instance when either is 'all'."""
    if region == "all" or env == "all":
        return [(r, e) for (r, e) in INSTANCES if region in (r, "all") and env in (e, "all")]
    return [(region, env)]


def main():
    spec = rs.parse("rundeck")
    jobs = _emit_visible(spec)                       # emit once, reuse per instance
    results = []
    for r, e in targets(spec.region.value, spec.env.value):
        inst = INSTANCES[(r, e)]
        cfg = config_from_env(               # token/CA from the env; the rest baked
            f"{r}-{e}", url=inst["url"], project=inst["project"], proxy=PROXY,
        )
        results.append(sync_target(cfg, jobs, prune=bool(spec.prune.value)))
    ok = all(x.ok for x in results)
    print({"ok": ok, "instances": len(results)})


def _emit_visible(spec):
    jobs = emit_jobs("ops", venv=spec.venv.value)
    names, hidden = discover_visibility()
    kept, _dropped = filter_visible(jobs, names, hidden)
    return kept
```

Its `runspec.toml` adds the two targeting args on top of the standard ones:

```toml
[rundeck]
description = "Sync runnables to our Rundeck fleet"

[rundeck.args]
region = {type = "choice", options = ["eu", "us", "all"], description = "Region to target"}
env = {type = "choice", options = ["uat", "prod", "all"], description = "Environment to target"}
```

`--region all` / `--env all` fans out to every matching instance behind one
command; a concrete pair targets one. Because the core stays single-target
(`sync_target`), the wrapper owns only the map and the loop — everything else
(emit, UUID identity, prune) comes from the published packages.

## What's in a job

Every job runs a single Bash **script step**. The script reads each option's
value into the runspec env var the runnable reads that arg from, through a
quoted heredoc:

```bash
IFS= read -r RUNSPEC_DEPLOY_ARG_TOKEN <<'RUNSPEC_VALUE'
x@option.token@
RUNSPEC_VALUE
RUNSPEC_DEPLOY_ARG_TOKEN=${RUNSPEC_DEPLOY_ARG_TOKEN#x}
```

Rundeck replaces each `@option.<name>@` token with the option's value before it
copies the script to the node. The script then runs the runnable with only the
binary and the subcommand path on its command line, for example
`/opt/app/.venv/bin/deploy start`. Every arg (named, flag, positional, `rest`,
`password`) reaches the runnable through the env var, which is how runspec
already resolves an arg that isn't on the command line.

- An option left **empty** isn't exported, so the runnable's own default
  applies.
- **Flags** arrive as `"true"` / `"false"`.
- A **`rest`** arg arrives as one value (it isn't word-split into separate
  items).
- A value can't contain a **line break**: the job refuses to run (exit code 64)
  rather than read one. See below for why.
- If the runnable isn't where the job expects it (`<venv>/bin/<runnable>`, or
  on the Rundeck user's `PATH` without a venv), the job stops with exit code
  127 and a message naming the runnable, the place it looked, the account that
  looked and the node:
  `runspec: deploy not found at /opt/app/.venv/bin/deploy (checked as rundeck on web-01)`.
  A venv whose `bin/` the Rundeck user can't open is left to the run itself.

### How values reach the runnable

The emitter avoids the two usual ways of passing values, because neither works
in a locked-down corporate fleet:

- **No `RD_OPTION_*` env vars.** Those only reach a remote node when its sshd
  has `AcceptEnv RD_*`, and Red Hat advises against setting that.
- **No arguments.** A value on a command line is visible to every user through
  `ps` (PII and secrets alike), and Red Hat advises against hiding it with the
  `hidepid` mount option of `/proc`.

That leaves the inline-script tokens. Rundeck before 5.20 substitutes them
without escaping (the
[option-escaping advisory](https://docs.rundeck.com/docs/history/cves/2025-07-option-escaping.html),
affecting 3.4.1 to 5.19), so a value written straight into a shell command
could run commands on the node. The script is built so that the shell never
parses a value, which makes it safe on every version, secure options included
(they can't carry a `regex`):

- Each token sits alone on its own line inside a heredoc whose delimiter is
  quoted (`<<'RUNSPEC_VALUE'`). Bash treats the body as plain data: quotes,
  `$(...)`, backticks and `;` in a value are just characters.
- A heredoc body ends only at a line that is exactly the delimiter. The `x` in
  front of each token means a value can never make its own line equal it, so
  the only way out is a value containing a line break.
- The script's third line counts the script file's own non-blank lines and
  exits with code 64 unless the count is the one the emitter wrote. A value
  with a line break adds a line, so the job stops before any value is read.
  Blank lines aren't counted, so a trailing newline changes nothing.

Use an option's `regex` / `values` to restrict what it accepts where you can.

!!! warning "Accepted tradeoff: values in the temp script"
    Rundeck writes the substituted script, secret values included, to a temp
    file on the node (for example under `/var/tmp`) and deletes it when the step
    ends. It copies the file with the node's default umask, so the script's
    first command is `chmod 700` on its own file: from then on only the Rundeck
    SSH user (and root) can read it. That also covers a file Rundeck fails to
    delete, such as after a killed job. Values never appear in `ps`, the sudo
    log or the Rundeck job definition.

### Running as the service account

Runnables should run as the service account that owns their venv, not as the
Rundeck SSH user. The job follows the same `run_as` rules as the runnable
itself, resolved in this order: `--run-as`, `[<runnable>.meta.rundeck] run_as`,
`[config.meta.rundeck] run_as`, the runnable's own `run_as`, `[config] run_as`,
and finally `"@owner"` when none of them is set (the core default).

| `run_as` | What the job does |
|---|---|
| `"@owner"` (or unset) | Looks up the owner of the venv on the node (`stat`). Runs directly when the Rundeck user already is that account or the venv is owned by root; otherwise runs through `sudo -n -u <owner>`. Without a `venv`, the venv is the one the runnable was found in on `PATH`. |
| `"svc"` (an account) | Runs through `sudo -n -u svc`, or directly when the Rundeck user already is `svc`. |
| `""` | Runs as the Rundeck SSH user, no `sudo`. |
| `"$ENV"`, a per-host table | Can't be resolved by a job, so it runs as the Rundeck SSH user. Set `run_as` in `meta.rundeck` instead. |

For example:

```toml
[config.meta.rundeck]
venv   = "/opt/app/.venv"
run_as = "svc"
```

Because `sudo` resets the environment, the values don't travel as env vars
across it. The script writes them on the command's **standard input** as the
runspec env frame, and the fixed env prelude on the venv's own `bin/python`
reads them back before it starts the runnable (the same mechanism the console
uses; see SPEC → "Remote Execution → Command building"):

```bash
rs_frame RUNSPEC_DEPLOY_ARG_DEPTH RUNSPEC_DEPLOY_ARG_TOKEN \
  | sudo -n -u "$rs_user" /opt/app/.venv/bin/python -c "$RS_PRELUDE" /opt/app/.venv/bin/deploy start
```

The sudoers rule therefore needs to allow the Rundeck SSH user to run the
venv's `bin/` as `svc` without a password, for example:

```
rundeck ALL=(svc) NOPASSWD: /opt/app/.venv/bin/
```

Without a `venv` the prelude runs on `sh` instead.

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
| `multiple = true` | **multi-valued** option (`multivalued: true`, `delimiter`); with `options`, also `values` + `enforced` (checkboxes) |

Required args set `required: true`; defaults are stringified into `value`.
A `multiple` arg's list default is joined with the option's delimiter
(`default = ["Missing", "Stale"]` → `value: "Missing,Stale"`). The delimiter is
the arg's own `delimiter` when it has one, else `,`; Rundeck joins the values
picked on the run form with it, and the job script turns each delimiter into a
line break, which is runspec's env form for a list, so the runnable gets
`["Missing", "Stale"]`. Emit fails if a default value contains the delimiter
(set a `delimiter` its values don't use). A multi-valued `int`/`float` option
has no `regex`: Rundeck would test the joined text, so runspec checks each
value instead. Range,
pattern, and positional/rest hints that Rundeck can't model are surfaced in the
option's description.

### Secrets

A `password` arg becomes a Rundeck **secure option** (`secure: true`,
`valueExposed: true`): the operator enters it at run time, it is encrypted, and
it is kept out of the job log. It reaches the script through its
`@option.<name>@` token like every other option, and the runnable reads it from
`RUNSPEC_<RUNNABLE>_ARG_<NAME>`, the env var runspec already reads passwords
from. It never appears on a command line. The temp-script tradeoff above
applies to it.

## Nodes and load-balancing

By default a job has no node filter and runs on the Rundeck server. Give it a
node filter and it dispatches to that node set with an **orchestrator random
subset of 1** — each run lands on one random node of the set, so a scheduled or
triggered job spreads its load:

```bash
runspec emit --rundeck --venv /opt/app/.venv --run-as svc --node-filter 'tags: web'
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
venv   = "/opt/app/.venv"
run_as = "svc"                     # sudo -n -u svc /opt/app/.venv/bin/…
group  = "myteam"

[deploy]
description = "Deploy the app"
require-command = true

[deploy.meta.rundeck]
venv        = "/opt/deploy/.venv"
run_as      = "svc-deploy"
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
| `run_as` | Account the command runs as: an account, `"@owner"` (default) or `""` — see "Running as the service account" |
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
| `--run-as <account>` | Run each job's command as this account, `@owner` or `""` (`sudo -n -u` when needed) |
| `--check` | Verify emitted files are up to date; write nothing. Exit 1 if stale |
| `--stdout` | Print the combined JSON array to stdout instead of writing files |

### Keeping jobs fresh in CI

Commit `jobs/` and guard drift the same way `runspec stubs --check` does:

```bash
runspec emit --rundeck --venv /opt/app/.venv --run-as svc --check
# ✗  jobs is out of date — run 'runspec emit --rundeck'   (exit 1)
```

## Not covered (yet)

- **`su`** — `run_as` always escalates with `sudo -n -u`.
- **Group constraints** (`exclusive`, `exactly-one`, …) have no Rundeck
  equivalent; they are noted in option descriptions and still enforced by the
  runnable at runtime.

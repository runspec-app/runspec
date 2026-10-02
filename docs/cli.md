# CLI reference

The `runspec` binary ships with both `runspec` (Python) and `runspec-node`.
It scaffolds new projects, validates configs, smoke-tests runnables for CI,
emits agent schemas, runs a live MCP server, and executes tools on remote hosts.

```
runspec <command> [options]
```

!!! tip "It's runspec all the way down"
    Both CLIs define their own command surface in `runspec.toml`. When you
    run `runspec --help`, you're seeing the same renderer your CLI gets —
    the same argument validation, the same `examples` field, the same
    inference. The bundled specs live at:

    - `packages/python/runspec/runspec/runspec.toml`
    - `packages/node/src/runspec.toml`

    Read either as a worked example of a non-trivial spec: subcommands,
    `examples`, `position`, `rest`, `choice`, short flags, and a `[config]`
    block that suppresses autonomy display on the developer-facing menu.

---

## Top-level options

| Option | Description |
|---|---|
| `-V`, `--version` | Print package version and exit |
| `-h`, `--help` | Show help (also shown when no command is given) |

---

## `runspec init`

Scaffold a new runnable: write `runspec.toml` and a working code stub.

```bash
runspec init [--name <name>] [--lang <lang>] [--example] \
             [--write-project] [--project-dir <dir>] [--force]
```

| Flag | Default | Description |
|---|---|---|
| `-n`, `--name <name>` | current directory name | Name for the initial runnable |
| `--lang <lang>` | `python` (Python CLI) / `typescript` (Node CLI) | Code stub language: `python`, `typescript`, `javascript` |
| `-e`, `--example` | off | Generate worked example runnables (`clean` + `scan`) with confirmation prompts, conditional deletion, autonomy escalation |
| `-w`, `--write-project` | off | **Python CLI only.** Also generate `pyproject.toml`, `__init__.py`, `.gitignore`, and `CLAUDE.md` in the parent directory |
| `-d`, `--project-dir <dir>` | parent directory | Where `--write-project` lays down its files |
| `--force` | off | Bypass the cwd safety check (don't refuse if `pyproject.toml` is already present) |

If `runspec.toml` already exists, `init` exits with an error — it will not
overwrite or merge. If a code stub already exists, it is skipped with an
informational message.

### What gets written

=== "Bare init"

    ```
    greet/
      runspec.toml
      greet.py            # or greet.ts / greet.js depending on --lang
    ```

=== "init --example"

    ```
    sandbox/
      runspec.toml        # defines clean + scan runnables
      clean.py            # destructive op with autonomy/confirmation
      scan.py             # read-only op marked autonomy = "autonomous"
    ```

=== "init --write-project (Python)"

    ```
    .                          # parent of cwd, by default
    ├── pyproject.toml         # [project.scripts] wired; [tool.mypy] mypy_path
    ├── .gitignore
    ├── .pre-commit-config.yaml # runspec stubs hook (keeps types fresh)
    ├── CLAUDE.md              # project memory for Claude Code
    ├── typings/
    │   └── runspec/__init__.pyi  # placeholder — "run runspec stubs"
    └── greet/
        ├── __init__.py
        ├── runspec.toml
        └── greet.py
    ```

`--lang typescript` and `--lang javascript` generate a `.ts` / `.js` stub
respectively. The Node CLI supports the same `--lang` values but `python` is
not in its menu (use the Python CLI to scaffold a Python project).

**`parse()` typing is wired from the start.** `init` drops a placeholder
`typings/runspec/__init__.pyi`, so before you've generated anything your editor
already says *run `runspec stubs`* (a typed arg access is a clear error, not a
silent `Any`). Once the package is installed, the next `runspec local`
auto-upgrades that placeholder to real types; afterwards `runspec local` nudges
when the stub is stale, and the `--write-project` pre-commit hook regenerates it
when a `runspec.toml` changes. See [`runspec stubs`](#runspec-stubs).

### Examples (from the bundled spec)

```bash
runspec init                                              # use cwd name as the runnable name
runspec init --name deploy                                # scaffold a runnable called 'deploy'
runspec init --example                                    # generate worked example (clean + scan)
runspec init --example --write-project                    # also generate pyproject.toml
runspec init --write-project --project-dir /tmp/myproject # write project files to a specific path
runspec init --name myapp --lang typescript               # use TypeScript code stub
```

---

## `runspec local`

List all runspec-aware runnables installed in this environment, with inline
validation. Also emits tool schemas for agents.

```bash
runspec local [--format <fmt>] [--runnable <name>]
```

| Flag | Default | Description |
|---|---|---|
| `-f`, `--format <fmt>` | `text` | Output format: `text`, `json`, `mcp`, `openai`, `anthropic` |
| `-r`, `--runnable <name>` | all | Filter to one runnable by name |

**Discovery** uses `importlib.metadata` (Python) or `node_modules/.bin/`
(Node) — no filesystem walk, no guessing. Runnables must be installed
(`pip install -e .` / `npm install`) to appear.

### Text output (default)

Shows installed runnables grouped by config file, with autonomy levels and
any config issues inline:

```bash
runspec local
```

```
Found 3 installed runnable(s):

  /home/user/project/mypkg/runspec.toml
    deploy       Deploy the application to an environment  [confirm]
    backup-logs  Back up application logs to S3            [manual]
    process      Process input files                       [confirm]

Issues:

  ℹ  'process' autonomy not declared — defaulting to 'confirm'
  ✗  'process.api-key' is required but has no description

Run 'runspec local --format mcp' to emit MCP tool schemas.
```

Exits with code 1 if any errors are found — usable as a CI gate:

```yaml
# .github/workflows/ci.yml
- name: Validate runspec
  run: runspec local
```

### Schema output

Emit all installed runnables as tool schemas for agent frameworks:

```bash
runspec local --format mcp          # MCP tool list (the standard schema format)
runspec local --format openai       # OpenAI tool calling format
runspec local --format anthropic    # Anthropic tool use format
runspec local --format json         # raw discovery JSON for tooling
```

Target a single runnable:

```bash
runspec local --format mcp --runnable deploy
```

Every emitted tool carries:

| Field | Source |
|---|---|
| `name` | Runnable name |
| `description` | `description` field |
| `x-autonomy` | Effective autonomy level |
| `x-autonomy-reason` | `autonomy-reason` if declared |
| `x-output` | `output` field (`"text"` if not declared) |
| `inputSchema` | JSON Schema for all args |

An argument that declares its own `autonomy` additionally carries it as
`x-autonomy` on its property inside `inputSchema`, so schema consumers can
apply per-arg escalation (most restrictive level wins when the arg is
provided) without reading the TOML.

---

## `runspec stubs`

Generate precise per-runnable types for `parse()`, so your editor and type
checker know each runnable's args. Scans the installed venv (same discovery as
`runspec local`) and writes a self-contained stub to
`typings/runspec/__init__.pyi` at the project root.

> For a step-by-step walkthrough and the everyday workflow, see
> [Typed args (`runspec stubs`)](typed-args.md).

```bash
runspec stubs [--check] [--off]
```

| Flag | Default | Description |
|---|---|---|
| `--check` | off | Verify the stub is up to date; write nothing. Exit 1 if missing/stale. |
| `--off` | off | Remove the stub, restoring the default `Any`-typed arg access. |

With the stub in place, `parse("<name>")` is typed precisely at the call site —
no annotation, no cast:

```python
from runspec import parse

args = parse("deploy")
args.workers.value        # int
args.format.value         # Literal["json", "yaml"]
args.wrokers              # error: "_DeployArgs" has no attribute "wrokers"; maybe "workers"?
```

A typo or an arg you haven't regenerated for is a hard error rather than a
silent `Any`. The runnable's metadata surface (`args.runspec_*`, `args.get(...)`,
`name in args`) is preserved, and subcommand args are exposed as the superset
across the command tree (read presence-dependent ones with `args.get(...)`).

**Editor setup.** pyright / Pylance (VS Code) read `typings/` by default — no
config. For mypy, point it at the directory:

```toml
# pyproject.toml
[tool.mypy]
mypy_path = "typings"
```

**Keeping it fresh.** The stub is generated from `runspec.toml`, so regenerate
when an interface changes — the same reflex as re-locking dependencies. Guard it
in CI or a pre-commit hook so a stale stub can't slip through:

```yaml
# .github/workflows/ci.yml
- name: Check runspec stubs are current
  run: runspec stubs --check
```

A stale stub never affects runtime — `parse()` reads the toml, not the stub — so
the worst case is an out-of-date hint until you rerun `runspec stubs`.

---

## `runspec test`

A CI smoke gate. Where `runspec local` validates *metadata* (descriptions,
autonomy, entry-point registration), `runspec test` proves each runnable
actually **works** — it's built for deployment packages that bundle runnables
from different authors, some of whom may not write tests.

```bash
runspec test [--format <text|json>] [--runnable <name>]
```

For every discovered runnable it runs two checks:

1. **Spec smoke** (in-process) — drives [`ParseHarness.smoke()`](testing.md):
   `--help` works for the runnable and every subcommand, `require-command` is
   enforced where declared, and omitting a required arg exits non-zero. This
   proves the `runspec.toml` is coherent and the parse wiring is sound.
2. **Entry-point `--help`** (subprocess) — executes the installed binary with
   `--help` and expects exit 0. This imports and runs the runnable's *own* code,
   catching import errors, syntax errors, and a wrong runnable name passed to
   `parse()` — failures the spec smoke can't see because it never loads that
   code. A missing binary or a 30s timeout counts as a failure.

It exits 1 if any runnable fails either check — drop it straight into CI:

```yaml
# .github/workflows/ci.yml
- name: Smoke-test all runnables
  run: runspec test
```

### Output

Text (default) prints a `✓`/`✗` line per runnable and a final tally; failures
show the entry-point error and the most informative stderr line:

```text
Tested 3 runnable(s):

  ✓  deploy                   spec ok, --help exit 0
  ✗  backup                   spec ok, --help failed
         entry point: exit 1
         stderr: ModuleNotFoundError: No module named 'boto3'
  ✗  scan                     spec failed
         spec: KeyError: runnable 'scan' not in ...

Summary: 1 passed, 2 failed (3 total)
```

`--format json` prints a machine-readable object (`results[]` + `summary`) to
stdout regardless of pass/fail, then sets the exit code — so CI can both gate
and parse:

```bash
runspec test --format json | jq '.summary'
```

```json
{
  "results": [
    {"runnable": "deploy", "source": "/abs/runspec.toml", "ok": true,
     "checks": {"spec_smoke": {"ok": true, "checks": ["--help", "..."]},
                "entry_point": {"ok": true}}}
  ],
  "summary": {"total": 1, "passed": 1, "failed": 0}
}
```

### Discovery scope

The set of runnables tested matches what `runspec local` discovers:

- **Python** tests every installed `runspec`-dependent package in the venv
  (`importlib.metadata`) — `pip install` (or `pip install -e .`) the packages you
  want covered.
- **Node** tests every runnable in the venv-shaped folder's single
  `runspec.toml`. A Node deployment folder *is* the unit (see
  [Deploying a Node runnable folder](node-deploy.md)); dependency specs under
  `node_modules` are not runnables and are not tested.

---

## `runspec serve`

Start a live MCP stdio server for the current environment.

```bash
runspec serve
```

No arguments. Reads the runspec config (walking up to find `runspec.toml`),
then starts a [Model Context Protocol](https://modelcontextprotocol.io)
server over stdin/stdout. Every installed runnable is exposed as an MCP
tool. When an agent calls a tool, `serve` runs the corresponding script and
streams the output back.

Zero extra dependencies — the protocol is JSON-RPC 2.0 newline-delimited over
stdin/stdout (plain stdlib).

### Discovery

| Pack | How runnables are found |
|---|---|
| Python | `importlib.metadata` — installed packages that declare `runspec` as a dependency |
| Node | Filesystem scan of cwd and subdirectories, plus `node_modules/.bin/` |

For Python, `pip install -e .` is the convention for making a package
visible during development.

### Subcommand flattening

Runnables that declare subcommands (`[<name>.commands.<sub>]`) are
automatically expanded into flat MCP tools with underscore-joined names:

```toml
[portal-api.commands.orders.commands.get-list]
description = "List orders"
autonomy    = "confirm"
```

Becomes the MCP tool `portal-api_orders_get-list`, with command
`[portal-api, orders, get-list, …args]` assembled at invocation time.

### Host filtering

If a runnable declares a `hosts` field, `serve` checks the current machine's
hostname at startup. Tools that don't match are excluded from the MCP tool
list. See [Jump Hosts](jump-hosts.md) for the remote-execution model.

### Environment variables on every invocation

Before running a script, `serve` injects:

```
RUNSPEC_<ARG_NAME_UPPERCASED>=<value>   # for every arg declared in the spec
RUNSPEC_AGENT=1                          # always, so the runnable can branch
RUNSPEC_CONFIG=/abs/path/to/runspec.toml # so parse() finds the spec in the subprocess
```

Hyphens become underscores; `flag`/`bool` values are `0` or `1`;
`multiple = true` lists are newline-delimited. Defaults are always set even
when the caller didn't pass the arg.

### Connecting to Claude Desktop

```json
{
  "mcpServers": {
    "analytics-pipeline": {
      "command": "/home/user/envs/analytics-pipeline/bin/runspec",
      "args": ["serve"],
      "cwd": "/home/user/projects/analytics"
    }
  }
}
```

On Windows:

```json
{
  "mcpServers": {
    "analytics-pipeline": {
      "command": "C:\\envs\\analytics-pipeline\\Scripts\\runspec.exe",
      "args": ["serve"],
      "cwd": "C:\\projects\\analytics"
    }
  }
}
```

See [Agent Integration](agents.md) for autonomy gating, schema fields, and
the `RUNSPEC_AGENT` pattern.

---

## `runspec jump`

List tools on a remote host or run a tool via SSH+MCP.

```bash
runspec jump [--bin <path>] [--format <fmt>]
             <host> [<tool>] [-- <tool-args>...]
```

| Flag / Argument | Description |
|---|---|
| `<host>` | SSH connection string — `user@host` or just `host` (positional) |
| `<tool>` | Tool to run on the remote (positional) |
| `--bin <path>` | Path to runspec binary on the remote. Basename must be `runspec`. Env fallback: `RUNSPEC_JUMP_BIN`. Default: `runspec` (relies on remote PATH). |
| `-f`, `--format <fmt>` | Output format for tool listings: `text` (default) or `json` |
| `-- <tool-args>...` | Args forwarded to the remote tool (`rest`-type) |

### Discover what's available

```bash
runspec jump user@prod.example.com              # list tools available on the remote
runspec jump user@prod.example.com --format json
```

### Run a tool

```bash
runspec jump user@prod.example.com deploy -- --env prod
runspec jump user@prod.example.com backup-logs -- --days 14 --dry-run
```

Everything after `--` is forwarded to the tool on the remote.

### Specify the remote binary

If `runspec` isn't on the remote shell's PATH (SSH runs non-login shells):

```bash
runspec jump user@prod --bin /opt/venv/bin/runspec deploy -- --env prod
# or
export RUNSPEC_JUMP_BIN=/opt/venv/bin/runspec
runspec jump user@prod deploy -- --env prod
```

### How it works

1. SSHes to `<host>` with `BatchMode=yes` (stdin/stdout are the JSON-RPC channel).
   All connection options (port, key, ProxyJump, etc.) come from `~/.ssh/config`.
2. Starts `runspec serve` on the remote via the resolved `bin` path.
3. Speaks MCP JSON-RPC over stdin/stdout.
4. Invokes `tools/list` (without `<tool>`) or `tools/call` (with `<tool>`
   and tool args).
5. Streams the response back to your terminal in real time; stderr from the
   remote is mirrored live.
6. Exits with the remote process's exit code.

See [Jump Hosts](jump-hosts.md) for `~/.ssh/config` examples, the trust model,
and the `run_as` / `become_method` / `become_flags` privilege-escalation matrix.

---

## `runspec env`

Show the resolved `.runspec_env` deployment file for a runnable — the path,
where that path was resolved from, and the variables it would inject at run
time. Useful for debugging deployment-time variable injection without running
the tool.

```bash
runspec env              # the default .runspec_env
runspec env deploy       # the file that applies to the 'deploy' runnable
```

```
Resolved .runspec_env for 'deploy':
  Path:   /opt/app/.runspec_env
  Source: [config] runspec_env

  DEPLOY_SERVER = web-01
  DEPLOY_TOKEN  = ****
```

The resolution order (per-runnable `runspec_env` → `[config] runspec_env` →
default location) is described under
[Environment variable fallbacks](format.md#environment-variable-fallbacks) and
the `.runspec_env` section of the format reference.

---

## `runspec audit`

Record, list or verify [audit events](audit.md) — the venv's append-only,
hash-chained ledger (`{venv}/logs/audit.jsonl`), also copied to syslog / the
Windows event log.

```bash
runspec audit record payment.refund --outcome succeeded -t order=A-1043 -d amount=12.50
runspec audit record deploy.start --strict   # exit 1 if it couldn't be recorded
runspec audit list                           # newest 50 events
runspec audit list --json -n 500             # one JSON record per line
runspec audit verify                         # check the hash chain (exit 1 if broken)
```

| Option | Description |
|---|---|
| `record EVENT` | Event name (lowercase, dotted) |
| `-o`, `--outcome` | Outcome word (`started`, `succeeded`, `failed`, …) |
| `-t`, `--target key=value` | What was acted on (repeatable) |
| `-d`, `--detail key=value` | Extra context (repeatable) |
| `--strict` | Exit 1 if no sink took the event |
| `-f`, `--file` | Ledger file to list/verify, or directory to record into |
| `-n`, `--limit` | Events to show for `list` (default 50) |
| `--json` | JSON output |

---

## `runspec deny`

Manage the venv **denylist** — a kill switch that disables runnables installed
in this virtual environment, by name, user, and/or hostname. Edits
`{sys.prefix}/runspec_denylist.toml`, which `parse()` reads on every run, so the
effect is uniform across CLI runs, entry points, and `runspec serve`.

```bash
runspec deny add --runnable deploy                    # disable one runnable (all users/hosts)
runspec deny add -r deploy -u svc-batch -H 'prod-*'   # disable deploy for svc-batch on prod hosts
runspec deny all --reason "change freeze"             # disable every runnable in this venv
runspec deny list                                     # show the current mode + rules
runspec deny mode warn                                # warn instead of refusing (enforce|warn|off)
runspec deny remove 2                                 # remove rule 2 (numbered by `deny list`)
runspec deny remove -r deploy -H 'prod-*'             # remove the rule added with these same lists
runspec deny enable                                   # undo `deny all`; keep the rules
runspec deny clear                                    # remove the denylist — re-enable everything
```

`--runnable`/`--user`/`--host` accept shell-style globs (`*`) and may be
repeated; an omitted dimension means "all". `runspec deny` is a core subcommand,
not a runnable, so it can never disable itself — `runspec deny clear` is the
guaranteed re-enable path on any host. See the [denylist guide](denylist.md) for
the file format and matching rules.

---

## `runspec rebrand`

Republish a runnable package wheel under **your own distribution name** — for a
private index such as Nexus — without changing a line of it. Only packaging
metadata is rewritten (distribution name, `*.dist-info` directory, re-pointed
dependencies, `RECORD` hashes); code, `runspec.toml`, entry points and the
import package stay byte-identical.

```bash
runspec rebrand runspec-linux==0.14.0 --name mycorp-linux          # download via pip's index config
runspec rebrand runspec_windows-0.9.0-py3-none-any.whl --name mycorp-windows
runspec rebrand runspec-linux --name mycorp-linux \
    --rename-dep runspec-linux-core=mycorp-linux-core -o dist/     # re-point a core dependency
```

| Option | Meaning |
|---|---|
| `<source>` | A `.whl` path, or a requirement to `pip download` (no deps, wheels only) |
| `--name`, `-n` | New distribution name |
| `--rename-dep OLD=NEW` | Re-point a dependency (repeatable; one that doesn't apply is reported and ignored) |
| `--set-version` | Override the version — normally leave upstream's |
| `--out`, `-o` | Output directory (default `dist`) |

See [White-label runnable packages](white-label-runnables.md) for the full
workflow, including a scheduled CI job that mirrors each new upstream release.

---

## `runspec logs`

View, prune, and compact per-invocation audit logs — the read and maintenance
side of `[config.logging] store = "per-run"` (see
[Logging → Per-invocation files](logging.md#per-invocation-files-shared-venvs)).
In per-run mode each invocation writes its own file with **no in-process
rotation**, so `runspec logs` is how you read them as one stream and how you do
retention.

### View — many files, one stream

```bash
runspec logs deploy                       # merged, timestamp-sorted, one record per line
runspec logs deploy --follow              # live tail across invocations
runspec logs deploy --since 1h            # only the last hour
runspec logs deploy --user alice          # only runs alice launched
runspec logs deploy --run <run_id>        # one invocation
runspec logs deploy --json                # raw JSON lines (for jq)
```

The view is a plain stdout stream that composes with the usual tools — it's
TTY-aware and SIGPIPE-clean, so piping to `grep`/`head`/`less` just works, and
process substitution makes the files look like a single file to anything that
wants a path:

```bash
runspec logs deploy | grep ERROR | less
awk '$2=="WARNING"' <(runspec logs deploy --since 1h)
```

### Status — what's on disk

```bash
runspec logs status                       # per-runnable file count + disk usage
runspec logs status deploy                # just one runnable
runspec logs status --json                # machine-readable inventory
```

`status` is the read-only inventory the runspec-console **Logs tab** reads to
show per-venv usage before offering compact/prune. The `--json` shape is
`{dirs, runnables:[{runnable, per_run_files, archives, total_bytes, oldest,
newest}], total_bytes, total_files}`. Like every `runspec logs` verb it runs
locally or over SSH, so the console drives one uniform interface against every
venv it manages.

### Prune / compact — retention you schedule

There is no automatic rotation in per-run mode, so an operator schedules
cleanup. Both verbs default to **all** runnables in the venv when none is
given, take `--dry-run` to preview, and **never touch a single-mode
`{runnable}.log`** — only per-run files and archives.

```bash
runspec logs compact --older-than 7d --gzip   # roll runs >7d old into a dated .gz archive
runspec logs prune   --older-than 90d         # delete files older than 90 days
runspec logs prune deploy --max-files 50 --dry-run     # preview keeping newest 50
runspec logs prune --max-total-size 5GB                # cap total size (oldest deleted first)
```

**Which directory** — `--scope`. runspec reads logs from two places: `{sys.prefix}/logs`
(this venv's own dir) and, only when the venv dir isn't writable, `~/logs` (the
running user's home fallback, shared by every venv that fell back there). To avoid
one venv's prune deleting another venv's files in that shared `~/logs`, the
destructive verbs are **venv-scoped by default**: `prune`/`compact` act on
`{sys.prefix}/logs` only unless you widen them. `status`/`view` still show the
union. Override with `--scope`:

```bash
runspec logs prune --older-than 90d                # this venv's dir only (default)
runspec logs prune --older-than 90d --scope home   # the shared ~/logs fallback only
runspec logs prune --older-than 90d --scope all    # both (the pre-0.56 behaviour)
```

Add `--json` to either verb (with or without `--dry-run`) to get a structured
result object instead of text — what the console parses to render a preview and
then the applied outcome.

A typical nightly cron line:

```cron
30 2 * * *  /opt/venv/bin/runspec logs compact --older-than 7d --gzip && \
            /opt/venv/bin/runspec logs prune   --older-than 90d
```

Archives stay JSON-lines (gzipped), so `runspec logs <runnable>` and the
console's History/Analytics keep reading them after compaction.

**Set up the logs inbox** — `runspec logs share --inbox` (runspec >= 0.66.1,
POSIX). A shared venv needs this once, run as its owner. It lets every other
account's runs (operators, root, other crontabs) reach the venv's history
instead of falling back to their own `~/logs`:

```bash
sudo -u <owner> {venv}/bin/runspec logs share --inbox   # create logs/inbox (01777, owned by the owner)
runspec logs share --no-inbox                            # opt this venv out (logs/.no-inbox marker)
runspec logs share --inbox --owner-only                  # …and make logs/ itself owner-only (0755)
```

Other accounts drop their per-run files into the inbox, and the owner sweeps
them into the runs index (`runspec history sweep`, and automatically on the
owner's own runs). Run as root, the command drops to the venv's owner first. It
refuses a non-owner with a one-line message naming the owner. `--inbox` leaves
`logs/`'s own mode alone unless `--group` / `--acl-user` are also given.
Walkthrough: [Shared venvs — the logs inbox](logs-inbox.md).

**The group alternative** — `runspec logs share` without `--inbox`. When more
than one account writes into a venv (a console operator plus the service account
a `run_as` runnable runs as), they must all be able to write the runs index
(`runspec_runs.db`) and audit logs into the *same* dir. `share` makes that dir
group-writable in one command:

```bash
runspec logs share --group consoles   # chmod 2775 (setgid) + chgrp to `consoles`
runspec logs share                     # group-writable + setgid, group unchanged
runspec logs share --group consoles --scope home   # share the ~/logs fallback instead
```

For each targeted dir it ensures the dir exists, `chmod`s it `2775`
(group-writable **and setgid**, so new files inherit the dir's group), optionally
`chgrp`s it to `--group`, and repairs the existing `runspec_runs.db` (`chgrp` +
`chmod 664`) — a dir `chmod` never touches a file already inside it. Scoping
matches `prune`/`compact`: an unset `--scope` is this venv's dir only. It is
**sudo-free** like the rest of the core CLI — it changes only what the invoking
user may change — so if you don't own the dir, run it as the owner yourself:
`sudo -u <owner> {venv}/bin/runspec logs share --group consoles`. Add the accounts
to the group first, or a non-member still falls back to `~/logs`.

**Grant a specific user without a group** — `--acl-user` (repeatable). On a host
where you have no root, you often can't add an account to the dir's group (that
needs root) — but the dir's **owner** can grant write directly with a POSIX ACL:

```bash
sudo -u <owner> {venv}/bin/runspec logs share --acl-user alice --acl-user svc
```

For each user this sets an **access** ACL (recursive — existing files + the runs
index) and a **default** ACL (new files inherit), so the named account can write
the dir with no group membership and no root. Best-effort: if `setfacl` or
filesystem ACLs aren't available the share still succeeds (the `--json` result's
per-dir `acl` field reads `unsupported: …`) and you fall back to the group route.
This is exactly what the runspec-console "Make shared" button passes for your own
login user when it escalates to the owner.

---

## `runspec exec`

Run one installed runnable and record the whole invocation — including the parts
a runnable can't record about itself. Built for unattended callers (cron, CI,
schedulers), and it's what `runspec-scheduler` puts on its crontab lines.

```bash
runspec exec backup -- --target prod                 # args after -- go to the runnable
runspec exec cron -- list                            # subcommands pass through too
runspec exec --label nightly --trigger schedule backup
runspec exec --lock ~/.cache/nightly.lock backup     # recorded 'skipped' if the lock is held
runspec exec --skip-if-running --timeout 600 report  # skip if already running; kill after 10 min
```

What it adds over running the binary directly:

- **A row for every run** in the runs index (`runspec history`), even when the
  runnable has no `[config.logging]` or fails before logging starts (bad args, a
  `run_as` refusal, not installed — exit 127).
- **The observed exit code.** A runnable's own record infers `1` on an uncaught
  exception and `0` otherwise, so `sys.exit(2)` would read as success.
- **Captured output.** stdout and stderr are passed through live *and* their last
  `--capture-bytes` (default 64 KiB) are appended to the run's log file as
  `exec_output` records, next to an `exec_result` record — so `runspec logs view
  --run <run_id>` and the console's History show them, and `runspec logs prune`
  cleans them up like any other log.
- **Recorded skips.** With `--lock PATH` (POSIX, race-free) or
  `--skip-if-running` (runs-index check), a run that can't start because the
  previous one is still going is recorded with status `skipped` and exits 0.
- **Labels.** `--label` tags the run; find them with
  `runspec history list --label <label>`.

The wrapper and the runnable share one run id (handed down in `RUNSPEC_RUN_ID`),
so each run is one row: the runnable's own details (command, args, log file) plus
the wrapper's label, trigger and observed outcome. The exit code is the
runnable's (124 on `--timeout`). See the design:
[`docs/design/runspec-exec.md`](design/runspec-exec.md).

---

## `runspec history`

Query and maintain the per-venv **runs index** (`runspec_runs.db`) — a small
SQLite index core writes beside the audit logs (`{sys.prefix}/logs`, falling back
to `~/logs`) at the start and end of every run. It's what makes the console's
History tab load fast and gives a live "what's running" view. It is a **derived
index**: the JSON-lines audit logs stay the source of truth, so the index can
always be rebuilt from them.

```bash
runspec history                                   # recent runs, newest first
runspec history running                           # what's running now (pid-checked)
runspec history list --runnable deploy --failed   # filter; add --json for JSON lines
runspec history list --label sched:nightly        # runs recorded by `runspec exec --label`
runspec history stats                             # index health: rows, size, span, stale rows
runspec history prune --older-than 30d            # drop finished rows older than 30 days
runspec history prune --max-rows 10000            # keep only the newest 10k rows
runspec history reap                              # crash 'running' rows whose process is gone
runspec history sweep                             # index finished runs from the logs inbox (venv owner)
runspec history reindex                           # rebuild the index from the audit logs
```

**You rarely need to run these.** Every run's finalize already does the
maintenance automatically: it reaps orphaned `running` rows, ring-prunes to the
`[config.history]` bounds (`retention_days` 90, `max_rows` 50000 by default), and
reclaims space incrementally. The verbs are for on-demand repair — shrinking a
retention after the fact, clearing orphans on a quiet venv, or rebuilding an
index that was deleted or damaged.

**Scope.** Reads (`list`/`running`/`stats`) cover every logs dir by default;
`prune`/`reap`/`reindex` act on this venv's own `{sys.prefix}/logs` only unless
you pass `--scope home|all` — so maintaining one venv never touches another's
index in the shared `~/logs`.

**Permissions.** On a shared venv the index belongs to whoever created it (often
a service account). A maintenance verb run by a user who can't write it fails
with a named `permission denied … (owned by <user>)` error rather than reporting
"0 rows"; run it as the owner (`sudo -u <owner> {venv}/bin/runspec history reap`).
Other accounts don't need to write the index at all once the venv has a logs
inbox ([`runspec logs share --inbox`](#runspec-logs)).

**The logs inbox (0.66.0+).** `list`/`running` also show runs still waiting in
`{venv}/logs/inbox` (other accounts' runs, not yet swept). Such a row carries
`"pending": true` and its kernel-attested `os_user`, and the index wins a
dedupe. `sweep` is owner-only; as root it drops to the owner first. It indexes
finished (or confirmed-dead) inbox runs and moves their files into `logs/`. A run
whose lock is still held is left alone. `stats` reports the inbox's state
(`"inbox"` in `--json`). Schema v6 adds `os_user`, `source` (`direct`/`inbox`)
and `exec_uid`.

**`--json`.** `list`/`running` print one row per line; `stats`/`prune`/`reap`/
`reindex` print a single result object (`{"verb", "count", "dirs": [...], "ok"}`),
which is what the runspec-console's **Logs → Runs index…** dialog drives.

**Change sequence (0.63.0+).** The index (schema v4) stamps every write with a
per-DB change sequence and keeps a short-lived tombstone for every deleted row,
so a reader can pull only what changed since its last read — the
runspec-console's History cache does this over SSH instead of re-reading every
venv's newest rows on each refresh. `stats` shows it as `change seq N (floor F,
T tombstone(s))` (`"sync"` in `--json`). It's maintained by SQLite triggers
stored in the DB file, so an older runspec writing the same venv's index still
keeps it correct. Tombstones are kept 30 days. Since 0.64.0 each row also
carries the run's log-event counts by level and its uncaught exception (type +
message), so readers can aggregate from the index rather than the log files.
The Python API is
`runspec.runsdb.changes_since(log_dir, db_id=…, seq=…)`; see
[the design](design/history-incremental-pull.md).

---

## Bash and shell runnables

Any executable on `PATH` can be a runspec runnable — bash, Python, Node,
Ruby, Go binary, whatever. `runspec serve` invokes it with all argument
values pre-exported as `RUNSPEC_*` environment variables, so the runnable
doesn't need a runspec library:

```toml
[backup-logs]
description = "Back up application logs to S3"
autonomy    = "confirm"

[backup-logs.args]
env     = {type = "choice", options = ["prod", "staging"], description = "Target environment"}
days    = {type = "int", default = 7, description = "Days of logs to retain"}
dry-run = {type = "flag", description = "Print what would happen without doing it"}
```

```bash
#!/bin/bash
set -euo pipefail

if [ "$RUNSPEC_DRY_RUN" = "1" ]; then
    echo "Would sync $RUNSPEC_DAYS days of $RUNSPEC_ENV logs"
    exit 0
fi

aws s3 sync "/var/log/app/$RUNSPEC_ENV" "s3://logs-$RUNSPEC_ENV" \
    --delete --exclude "*.tmp"

echo "Backed up $RUNSPEC_DAYS days of $RUNSPEC_ENV logs"
```

Hyphens in arg names become underscores in the env var name (`dry-run` →
`RUNSPEC_DRY_RUN`). `RUNSPEC_AGENT=1` is always set so the script can branch
between human and agent output.

The runnable just has to be on `PATH` and have its section in
`runspec.toml`. `pip install` / `npm install` puts entry points on `PATH`
automatically; for bare shell scripts, drop them in your venv `bin/`
(Python) or expose them via a `bin` entry in `package.json` (Node).

---

## Usage in agent workflows

```bash
# 0. New project? Scaffold the whole thing
runspec init --name myapp --write-project

# 1. Check your config and see what's installed
runspec local

# 2. Preview what schemas an agent will see
runspec local --format mcp

# 3. Start the live MCP server
runspec serve

# 4. From an agent or terminal, run a tool on a jump host
runspec jump prod-app deploy -- --env prod
```

To wire it into Claude Desktop, point the MCP server config at `runspec
serve` (see the example above). The agent connects once at startup and
calls tools as needed.

---

**Next:** [Agent integration (MCP)](agents.md) — expose runnables as agent tools.

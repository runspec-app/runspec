# runspec Format Specification

Version: 1  
Status: Draft

This document is the canonical definition of the runspec format.
All language pack implementations are tested against this specification.

---

## Overview

A runspec defines the interface of anything runnable — a script, application,
or MCP tool. It lives in `runspec.toml` inside the package directory, alongside
the code it describes.

```
mypkg/
  runspec.toml    ← lives here, not at the project root
```

This location means build backends include it automatically as package data,
and `importlib.metadata` can locate it after install — no extra configuration needed.

---

## File Lookup Order

Implementations must use the following lookup strategy depending on context:

**Runnable discovery (`runspec local`, `runspec serve`):**
1. Locate via `importlib.metadata` — find `runspec.toml` in installed package files
2. Any package that declares `runspec` as a dependency is a candidate
3. Packages must be installed (`pip install` or `pip install -e .`) to be visible.
   There is no filesystem-scanning fallback; install is the convention.

---

## Top-Level Structure

```toml
[config]     # optional project-wide defaults
[<name>]     # one section per runnable
```

---

## The `[config]` Section

Project-wide defaults. All fields are optional.

| Field | Type | Default | Description |
|---|---|---|---|
| `autonomy-default` | string | `"confirm"` | Autonomy when unspecified on a script |
| `run_as` | string or table | `"@owner"` | Default `run_as` for runnables that don't declare one — same forms as the runnable field. See [`run_as`](#run_as). |
| `enforce_run_as` | string | `"error"` | Default `enforce_run_as` for runnables that don't declare one. See [`enforce_run_as`](#enforce_run_as). |
| `lang` | string | — | Preferred language for `runspec generate` |
| `name` | string | — | MCP server name reported by `runspec serve`. Defaults to the venv directory name. The venv directory name must pass the naming check (see *Virtual Environment Identity*) regardless of whether this field is set. |
| `version` | string | `"1"` | runspec spec version |
| `logging` | table | defaults | Logging configuration. On by default — an absent section means the defaults. See [Logging](#logging). |
| `audit` | table | defaults | Where audit events go besides the venv ledger. See [Audit Events](#audit-events). |
| `history` | table | defaults | The runs index (`runspec_runs.db`, beside the run logs). Fields: `db` (bool, default `true` — `false` skips the index; History falls back to parsing the logs), `retention_days` (int, `90`), `max_rows_per_runnable` (int, `50000` — each runnable keeps at most this many of its own rows, so one busy runnable can't push the others out; `0` = no per-runnable cap) and `max_rows` (int, `1000000` — the whole venv's ceiling) bound the ring-buffer prune, `max_run_hours` (int, `24`) is the age after which a `running` row a crash left behind is reaped. |
| `runspec_env` | string | — | Path to a `.runspec_env` file. Relative paths resolve from `sys.prefix`. Per-runnable `runspec_env` wins. |
| `meta` | table | — | Project-wide developer-defined metadata. Pass-through — runspec never interprets it. Orchestration emitters read project defaults here (e.g. `[config.meta.rundeck]` for `runspec emit --rundeck`). |

### Virtual Environment Identity

`runspec serve` derives the runnable group identifier from the execution
environment name at startup:

| Runtime | Source |
|---|---|
| Python | `Path(sys.prefix).name` — the venv root directory name |
| Node | `name` field from the nearest `package.json` |

This name identifies which set of runnables came from which environment. When
two environments expose a runnable with the same name, the group label is what
lets operators and agents tell them apart.

**Naming requirement (Python):** The following venv directory names are
rejected — `runspec serve` refuses to start if `Path(sys.prefix).name` matches
any of them:

```
venv  .venv  env  .env  virtualenv  .virtualenv
```

These names are ambiguous — they describe nothing about what is installed or who
owns the environment. A venv named `myapp-prod` makes an unambiguous claim that
`venv` cannot.

This check is unconditional. Setting `[config] name` does not bypass it — the
venv directory name is the execution identity, not a display label. A display
label over a generically-named venv is still a generically-named venv.

**Remedy:** Recreate the venv with a meaningful name that reflects the
application and deployment tier:

```bash
# wrong
python -m venv .venv

# right
python -m venv myapp-prod
source myapp-prod/bin/activate
pip install -e .
```

Deployments managed by configuration management should encode the application
name in the venv path so the identity is visible at the filesystem level:

```
/opt/myapp-prod/          ← venv root (sys.prefix basename = "myapp-prod")
  bin/runspec
  lib/python3.x/...
```


### Logging

Logging is **on by default**. Runnables are both CLI and agent tools, so every
run is recorded: the language pack configures the stdlib logging system
automatically when `parse()` is called, whether or not the runnable has a
`[config.logging]` section — an absent section means all the defaults below. No
additional imports or setup calls are required in the runnable —
`logger = logging.getLogger(__name__)` just works.

(Before Python core 0.71.0 logging was opt-in: nothing was configured without a
`[config.logging]` section. The Node pack still behaves that way until its port
lands.)

Logging has two layers:

- **The run audit** — the `run_start` / `run_summary` records, the runs index,
  the logs inbox for other accounts, and [audit events](#audit-events). Always on;
  there is no package-level switch.
- **The logging takeover** — handlers on the root logger, INFO → stdout /
  WARNING+ → stderr routing, capture of `print()` output into the log file, the
  auto-added `--debug` flag, and the uncaught-exception handler. On by default;
  `enabled = false` turns it off for runnables that bring their own logging setup.

With `enabled = false` (**audit-only**) the language pack leaves the process's
logging alone: no root handlers or level change, no stream routing, no print
capture, no `--debug` flag, and the uncaught-exception hook only records the
exception before calling whatever hook was installed before it. The run record
and audit events are still written to the log file, through runspec's own
file-only loggers (`runspec.runsummary`, `runspec.exception`, `runspec.audit`),
which don't propagate to the root logger in that mode.

Two exceptions to the default: the core `runspec` CLI (the control plane — it
records its state-changing verbs as audit events instead), and the
`RUNSPEC_AUDIT=off` environment variable, which restores the opt-in behaviour
(and makes `audit()` a no-op) for **test suites and CI only**. A declared
`[config.logging]` section is always honoured.

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | `false` = audit-only (see above). The run audit itself can't be turned off. |
| `rotate` | string | `"midnight"` | Rotation policy: `"N MB"`, `"N KB"`, `"N GB"` (size-based), `"daily"`, `"midnight"`, `"weekly"` (time-based). Inert when `store = "per-run"`. |
| `keep` | int | `7` | Number of rotated backup files to retain. Inert when `store = "per-run"`. |
| `summary` | bool | `true` | **Ignored since 0.71.0** (still accepted). The run-summary record is part of the always-on run audit. See *Run summary* below. |
| `store` | string | `"single"` | Log file layout — `"single"` or `"per-run"`. See *File layout* below. |

There is no `level` knob. Console routing is fixed (see below); the file
handler follows the same `--debug` toggle as stdout — INFO by default,
DEBUG when `--debug` is set. Silencing INFO on the console would break
agent responses (stdout is the MCP tool response body), so the threshold is
not configurable. Use the auto-added `--debug` flag to *raise* verbosity
everywhere at once.

Log file path is always: `{installation_root}/logs/{runnable_name}.log`.
Fallback when the root is not writable: `{home}/logs/{runnable_name}.log`;
if that isn't writable either, a private temporary directory (reported once
on stderr) — logging never makes a run fail.
One logs directory per environment — survives reinstalls and avoids
scattering logs across package directories.

| Language pack | `installation_root` |
|---|---|
| Python (`runspec`) | `sys.prefix` — the venv root |
| Node (`runspec-node`) | nearest ancestor `package.json`, skipping `node_modules` — the project root |

#### File layout

`store` selects how the audit file is laid out:

- **`"single"` (default):** one rotating file per runnable,
  `{installation_root}/logs/{runnable_name}.log`, rotated in-process per
  `rotate`/`keep`. Correct for single-user / local use.
- **`"per-run"`:** one file per invocation,
  `{installation_root}/logs/{runnable_name}.{utc-ts}.{run_id}.log`, where
  `utc-ts` is `YYYYMMDDThhmmssZ` and `run_id` is the same UUID injected into
  every record's `run_id` field. There is **no in-process rotation** — each
  invocation owns its own file, so the layout is safe for multiple users and
  for the same user running in parallel (no shared file means no rotation race
  and no cross-user ownership conflict). `rotate` and `keep` are inert;
  retention is an operator action (`runspec logs compact` / `prune`) or a
  [retention policy](#log-retention-policy-python-pack) the venv applies itself.

`"per-run"` is opt-in; the default stays `"single"` so existing deployments are
unaffected. **`"single"` is only honoured by the account that owns the logs
directory** (always on Windows): any other account writing into a shared logs
directory logs per-run instead, and a `"single"` file that can't be opened
(another account's file) also falls back to per-run rather than failing the
run. Both layouts emit identical JSON records (including the run-summary
and uncaught-exception records), so the only difference is how records are
distributed across files.

#### Shared logs directory — the inbox (Python pack)

A logs directory shared by several accounts (a service-account-owned venv run
by operators, by root, or from other accounts' crontabs) has **one writer for
its contents**: the account that owns the installation root ("the owner").
Everyone else drops their per-run file into a sticky, world-writable
subdirectory:

```
{installation_root}/logs/         owner-only writes (incl. the runs index)
{installation_root}/logs/inbox/   mode 01777, owned by the owner
```

- **Non-owners** (any account whose effective uid differs from the owner's —
  root included) write to `logs/inbox/` whenever it exists and is sane (a real
  directory, sticky + other-writable, owned by the owner, on the same
  filesystem). Their file is always per-run, whatever `store` says, uses the
  per-run name grammar, is created exclusively (never following a symlink,
  never reopened by path) with mode `0644`, and is held under an exclusive
  advisory lock for the life of the process. They never write the runs index.
  With no usable inbox the previous fallback (`~/logs`, then a temp dir)
  applies.
- **The owner and root create the inbox automatically** (unless a
  `logs/.no-inbox` marker file opts out): a missing inbox is created `0700`,
  handed to the owner, then set to `01777`. A path that is a symlink, not a
  directory, or owned by another account is never touched. The documented setup
  step is `{installation_root}/bin/runspec logs share --inbox`, run as the owner.
- **The owner sweeps** finished inbox files into the runs index and moves them
  into `logs/` (`runspec history sweep`, and opportunistically at the end of
  the owner's own runs). A file whose lock is still held is a live run and is
  never indexed; readers show index ∪ inbox.

Two records support this. Neither is counted in the run summary's `events`:

- **`run_start`** — the first record of an inbox file (on the
  `runspec.runsummary` logger): `run_id`, `runnable`, `command_path`, `pid`,
  `host`, `proc_start`, `user`, `user_target`, `invocation_args`, `start_ts`,
  `label`, `trigger`, `venv`, and `integrity` / `integrity_files` (see
  [Installed-Code Integrity](#installed-code-integrity)). Omitted when `[config.history] db = false`; a
  `{"event": "run_start", "history_db": false}` marker is written instead.
- **`run_end`** — a slim end record (`run_id`, `exit_code`, `duration_ms`)
  that packs before 0.71.0 wrote instead of `run_summary` when `summary =
  false`. Sweeps still accept it; current packs always write `run_summary`.

The Node pack does not implement the inbox yet: its log directory is the
project root and it keeps no runs index.

#### Log retention policy (Python pack)

Per-run files and the archives `runspec logs compact` makes are never rotated,
so a shared logs directory grows until something removes them. An
installation can carry a **retention policy** that it applies to itself:

```
{installation_root}/runspec_logs_retention.toml
```

```toml
[logs]
compact_after  = "7d"    # roll per-run files older than this into a gzip archive
delete_after   = "90d"   # delete per-run files and archives older than this
max_total_size = "5GB"   # optional: delete the oldest files over this total
```

- Every key is optional; at least one must be set. Durations use the
  `runspec logs` grammar (`30m`, `24h`, `7d`, `2w`), sizes `500KB`/`10MB`/`5GB`.
  When both are set, `compact_after` must be shorter than `delete_after`.
- The policy covers only `{installation_root}/logs/` — never `~/logs`, the
  inbox, `audit.jsonl`, the runs index or a `"single"` `{runnable}.log`
  (which rotates itself). It acts on exactly the files `compact` and `prune`
  manage, and records the same `logs.compact` / `logs.prune` audit events.
- An archive's modification time is the newest run folded into it, so
  `delete_after` counts from the newest run in the archive.
- **Who applies it:** at the end of a run by the owner of the installation
  root (the account that writes `logs/`), at most once every 24 hours, in a
  detached background process so the run's exit is never delayed. A policy
  file owned by any account other than the owner or root, or writable by group
  or others, is ignored. Runs by other accounts never apply it, so a venv that
  only other accounts run waits for the owner's next run (as the inbox sweep
  does).
- The outcome of the last pass is written to
  `{installation_root}/logs/.retention-last.json` (`ts`, `compacted`,
  `deleted`, `freed_bytes`, `error`); its modification time is the 24-hour
  clock. `logs/.retention.lock` keeps two passes from overlapping.
- `runspec logs retention` shows the policy and the last pass; with
  `--compact-after` / `--delete-after` / `--max-total-size` it sets them (a
  value of `off` clears one), `--off` removes the policy, and `--now` applies
  it immediately (`--dry-run` previews). Root drops to the owner first.

The Node pack has no retention policy.

#### Run labels

Every run records a **label** saying where it was launched from: the
`label` field of `run_start` and `run_summary`, and the runs index's `label`
column. `parse()` reads it from `RUNSPEC_LABEL`; a run with no `RUNSPEC_LABEL`
was started from a shell, cron or a script and records `cli`. Launchers set it:

| Label | Set by |
|---|---|
| `cli` | nothing (the default) |
| `console` | runspec-console, for every run it starts (whether a person or its agent asked for it is the separate `agent` field) |
| `mcp` | `runspec serve` and the runspec-mcp gateway |
| `rundeck` | jobs written by `runspec emit --rundeck` |
| `ansible` | modules written by `runspec emit --ansible` |
| any | `runspec exec --label <label>` |

`runspec exec` records `--label`, else the caller's `RUNSPEC_LABEL`, else `cli`,
and hands the same label to the child in `RUNSPEC_LABEL`. Once a row has a
label, a later write to the same run never replaces it, so a child's own label
never overwrites the wrapper's. The label is not removed from the environment,
so a runnable started by another runnable records the same label.

The Node pack does not record labels yet.

#### Console routing

A single `logger.X` call works in both CLI mode and agent mode — the language
pack routes by record level so the dev never has to branch on context:

| Level | Stream | Format | Notes |
|---|---|---|---|
| `DEBUG` | stdout | `DEBUG file.py:42: message` | Only included when `--debug` is passed — applies to the file handler too |
| `INFO` | stdout | `message` | Plain — reads like `print()` |
| `WARNING` | stderr | `WARNING: message` | Prefixed so it stands out |
| `ERROR` | stderr | `ERROR: message` | Prefixed so it stands out |
| `CRITICAL` | stderr | `CRITICAL: message` | Prefixed so it stands out |

The file handler is unconditional and always JSON. Its level follows the
same `--debug` toggle as stdout — INFO by default, DEBUG when `--debug` /
`RUNSPEC_DEBUG=1` is set. One knob: stdout and the audit file move
together. Defaulting to INFO keeps third-party library DEBUG noise out of
the log file. Stderr is independent and stays pinned at WARNING.

The split matches Unix stream conventions, which makes the same `logger.info()`
call do the right thing in two very different contexts:

* **CLI mode** — stdout shows the human-readable output and remains pipeable;
  stderr carries warnings/errors that wouldn't corrupt a downstream pipe.
* **Agent mode** (`RUNSPEC_AGENT=1`) — `runspec serve` captures stdout as the
  MCP tool response, so `logger.info` calls appear in the response the agent
  sees. Stderr is captured separately and surfaced when the runnable exits
  non-zero.

> **Pipe-emitting runnables.** A runnable that writes machine-readable data on
> stdout for piping (e.g. `my-tool | jq`) must keep stdout clean. Use `print()`
> (or its equivalent) for the data payload, and only call `logger.warning` and
> above — those go to stderr and don't corrupt the pipe. `logger.info` would
> land on stdout and mix with the data.

#### `--debug` flag

When logging is enabled (the default — not in audit-only mode), a `--debug` flag is auto-added to every
runnable (also settable via `RUNSPEC_DEBUG=1`). Passing it includes DEBUG
records — on stdout *and* in the audit file — and tracebacks on stdout.
One knob; both surfaces move together:

```
my-script --debug
RUNSPEC_DEBUG=1 my-script
```

The flag only *adds* visibility; it never silences anything. If your runnable
defines its own `debug` argument, yours is kept and no flag is added.

#### `run_id`

Each invocation gets a UUID4 assigned at `configure_logging()` time and injected
into every JSON log record as `extra.run_id`. When multiple operators run the
same runnable concurrently, log records from different runs are interleaved in a
single log file; `run_id` is the key that separates them cleanly.

The `run_id` field appears in every file-handler record (not console output) and
in the run_summary audit record.

#### `print()` capture

Any line written to stdout via `print()` (or equivalent) is captured and
forwarded to the `runspec.print` logger at INFO level, marked `_from_print=True`.
The file handler records it; the stdout console handler suppresses it to prevent
double-printing. This means runnables that use `print()` for user-visible output
appear fully in the audit log without any code changes.

#### Run summary

The language pack always records one summary at process exit — without any user code. It does two things:

1. **Audit log:** one JSON record on the `runspec.runsummary` logger,
   written to the audit file (and only the audit file — the console
   handlers filter this logger out).
2. **Human line:** one short line to stderr.

The audit record is fixed-schema:

```json
{"ts": "...", "level": "INFO", "logger": "runspec.runsummary",
 "message": "run completed",
 "extra": {
   "event": "run_summary",
   "run_id": "a3f8c2d1-...",
   "runnable": "compress",
   "command_path": [],
   "duration_ms": 1483,
   "exit_code": 0,
   "agent": false,
   "autonomy": "confirm",
   "exception": null,
   "events": {"DEBUG": 0, "INFO": 12, "WARNING": 2, "ERROR": 0, "CRITICAL": 0},
   "invocation_args": {"quality": 85, "dry_run": false},
   "arg_sources": {"quality": "cli", "dry_run": "spec_default"}
 }}
```

A run whose installed code was changed or came from outside a package index
also carries `"integrity": "modified"` (with `"integrity_files"`) or
`"integrity": "local"` — see
[Installed-Code Integrity](#installed-code-integrity).

The stderr line reads like a database client's closing message:

```
runspec: compress completed in 1.48s — 12 events (2 warn, 0 err)
runspec: compress failed in 0.91s — exit 1, ValueError — 5 events (1 warn, 1 err)
```

The level words are abbreviated (`warn`/`err`, never `warning`/`error`) so the
line doesn't trip log processors that flag any case-insensitive `error` match —
e.g. Rundeck's output highlighter, which would otherwise colour a clean run red
and tag it at ERROR level for downstream consumers.

The structured record is part of the run audit and is always written.
`summary = false` is still accepted, so older files load, but it is ignored
since 0.71.0 (the Node pack still honours it until its port).
`summary_console` (default false) gates the human-readable stderr line. There
is no per-invocation override flag.

In agent mode, `runspec serve` returns the same information per call via
the MCP `_meta` extension:

```json
"result": {
  "content": [...],
  "isError": false,
  "_meta": {"runspec": {"tool": "compress", "duration_ms": 1483, "exit_code": 0}}
}
```

`_meta` is the MCP-standard extension point — clients that don't read it
ignore the block. The tool response body is unaffected.

#### Uncaught exceptions

When logging is enabled (the default — not in audit-only mode), the language pack installs a process-level
handler (Python `sys.excepthook`; Node `uncaughtException` / `unhandledRejection`)
so **every** uncaught exception is handled the same way — automatically, with no
per-runnable `try`/`except` and no code template. A runnable just lets the
exception propagate.

The handler is installed at the *start* of `parse()` — not only once parsing finishes. This means a failure inside runspec's
own parse pipeline (argument inference, validation, type coercion, or a later
access to an undeclared argument) is routed through the same path as a runtime
exception, rather than dumping a raw traceback. The structured audit record (1.
below) is only written once logging is fully configured; a failure during the
parse pipeline itself — before handlers exist — still gets the console treatment
(2.) but no audit record, since there is no log file to write to yet.

On an uncaught exception:

1. **Audit log (always):** one structured record on the `runspec.exception`
   logger, written to the audit file regardless of `--debug`. Like `runspec.runsummary`, this logger is file-only —
   the console handlers filter it out. The record carries the human `exc` string
   (full traceback) **and** a machine-readable `exc_structured` object that UI
   tools can render in a table:

   ```json
   {"ts": "...", "level": "CRITICAL", "logger": "runspec.exception",
    "message": "uncaught exception",
    "exc": "Traceback (most recent call last):\n ...full string...",
    "exc_structured": {
      "type": "ValueError",
      "message": "invalid release tag 'v9.9.9-bad'",
      "module": "release",
      "frames": [
        {"file": "/abs/path/deploy.py",  "line": 48,  "func": "main",            "code": "main()"},
        {"file": "/abs/path/release.py", "line": 212, "func": "resolve_release", "code": "raise ValueError(...)"}
      ]
    },
    "extra": {"run_id": "..."}}
   ```

   `frames` is the full call stack, innermost last; `file` is an absolute path so
   a UI can offer click-to-open. (Node cannot recover the source `code` line from
   a stack string, so `code` is `null` there.)

2. **Console (debug-gated):** with `--debug`, a neat, aligned traceback is
   written to stderr — internal runspec frames are filtered out of the *display*
   (the full trace is still in the audit file):

   ```
   ValueError: invalid release tag 'v9.9.9-bad'

     deploy.py:48        main()
     release.py:212      raise ValueError(f"invalid release tag {tag!r}")
   ```

   Without `--debug`, only a single concise line is written to stderr:

   ```
   ERROR: ValueError: invalid release tag 'v9.9.9-bad'  (run with --debug for traceback)
   ```

This reuses the existing `--debug` knob — uncaught exceptions behave like every
other record (verbose on the console only with `--debug`, always full in the
file). It is **not** chained to the language's default handler, so the full
traceback no longer dumps to the console unless `--debug` is set.

#### Sensitive data filtering

All log output (console and file) is filtered for sensitive data before emission.
Passwords, tokens, API keys, bearer/basic auth headers, URL credentials, and
common JSON/form-encoded credential fields are replaced with `[REDACTED]`.
Filter errors are silent — a failing pattern never suppresses the log record.

#### Example

```toml
[config.logging]
rotate = "midnight"
keep   = 7
```

---

## Runnable Definition

Runnables are defined at the top level of `runspec.toml`. The reserved name
`config` is excluded — everything else is a runnable.

```toml
[<name>]
description     = "Human and agent readable description"  # recommended
autonomy        = "confirm"                               # optional
autonomy-reason = "Why this level was chosen"             # optional
output          = "text"                                  # optional
serve           = true                                    # optional, default true; also false or ["local","remote"]
discoverable    = true                                    # optional, default true; false hides from discovery
run_as          = "username"                              # optional, default "@owner" (the venv's owner), see Remote Execution
enforce_run_as  = "error"                                 # optional, default "error"; also "warn", "off"
become_method   = "sudo"                                  # optional, default "sudo"
become_flags    = "-H"                                    # optional
examples        = [...]                                   # optional, see Examples
runspec_env     = "/path/to/.runspec_env"                 # optional, see .runspec_env File
meta            = { ... }                                 # optional, developer-defined pass-through
```

`meta` is a runnable-level pass-through table (parallel to arg-level `meta`):
runspec never interprets it. It is where orchestration emitters read
per-runnable settings — e.g. `[<name>.meta.rundeck]` for `runspec emit
--rundeck` (venv, node filter, schedule, notifications). Project-wide defaults
go under `[config.meta.rundeck]`.

`[<name>.meta.scheduler.<schedule>]` is read by runspec-scheduler: one table
per schedule the runnable offers to any venv it is installed in (schedule id
`<name>.<schedule>`). Exactly one of `at` (`"02:30"`, `"midnight"`, `"noon"`,
with `on` = `daily` / `weekdays` / `weekends` / days like `mon,wed` or
`tue-thu` / a day of the month / `last`), `every` (`"15m"`) or `cron`; plus
optional `timezone` (`"local"` by default: the venv's `[scheduler] timezone`,
else the host's; or `"UTC"` / an IANA name), `spread` (each host fires at a
fixed offset inside the window), `args`, `command`, `timeout`, `catch_up` and
`description`. The venv decides whether they run (`[scheduler]
package_schedules` = `offer`, the default, lists them switched off; `auto`;
`off`). The schema checks the table's shape; runspec itself ignores it.

```toml
[cleanup-tmp.meta.scheduler.nightly]
description = "Remove old files from /tmp"
at = "02:30"
spread = "30m"
args = {older-than = "7d"}
```

### `examples`

A list of usage examples rendered by `--help`. Canonical form is inline TOML
tables with `cmd` (required) and `description` (optional):

```toml
[mytool]
examples = [
  {cmd = "mytool",                description = "Run with defaults"},
  {cmd = "mytool --verbose",      description = "Show debug output"},
  {cmd = "mytool --input data.csv"},
]
```

Bare strings are accepted as shorthand for `{cmd = <string>}`. Examples are
declarative; the spec layer never executes them.

### `serve`

Controls whether `runspec serve` exposes this runnable as an MCP tool.

**Forms:**

| Value | Meaning |
|---|---|
| `true` (default) | Always exposed — included in every `runspec serve` invocation |
| `false` | Never exposed — excluded from all `runspec serve` invocations |
| `["local"]` | Exposed only when served locally (not over SSH) |
| `["remote"]` | Exposed only when served over SSH |
| `["local", "remote"]` | Always exposed — equivalent to `true` |

**Context detection:**

`runspec serve` determines its own context at startup:

- If `RUNSPEC_SERVE_CONTEXT` is set in the environment, its value is used
  (must be `"local"` or `"remote"`).
- Otherwise: `"remote"` if `SSH_CONNECTION` is set (injected by the SSH
  daemon), `"local"` if not.

```toml
[my-launcher]
serve = false           # never exposed to agents
description = "Launch the interactive UI"

[setup-keys]
serve = ["local"]       # exposed only when running on the local machine
description = "Generate SSH keys for jump hosts"

[run-report]
serve = ["remote"]      # exposed only on remote machines
description = "Generate a server-side report"
```

`serve` only affects `runspec serve`. The runnable is still:
- visible in `runspec local`
- parseable via `rs.parse()` / `loadSpec()`
- callable directly from the command line

**Validation:** `runspec serve` exits with a clear error at startup if `serve`
contains an unknown context string.

Default when unspecified: `true`.

**Tool names.** `runspec serve` names each tool after its runnable, and a
runnable with commands becomes one tool per leaf command, joined with `_`
(`cluster_node_drain`). The MCP specification allows tool names of 1–128
characters. A name longer than 128 is never cut: it keeps its first characters
(any trailing `_` or `-` dropped), then `_` and the first six hex characters of
the SHA-1 of the full name, at most 128 in all. The result is the same on every
run and differs for two names that share a long start. `runspec serve` writes a
warning naming the tool to stderr, and `runspec test` reports the same warning
(it does not fail the run). Gateways that add a host or venv prefix (runspec-mcp,
the runspec-console MCP server) apply the same rule to the prefixed name.

### `discoverable`

Declares **whether** this runnable is listed by the discovery surfaces. It is a
plain boolean. Where `serve` controls *the context* a runnable is exposed in
(`local` vs `remote`), `discoverable` controls whether it is listed at all. The
two are orthogonal filters that compose.

| Value | Meaning |
|---|---|
| `true` (default) | Visible to the discovery surfaces — `runspec local` and `runspec serve` (MCP tools). |
| `false` | Hidden from all discovery surfaces — an internal helper. Still parseable and still runnable when invoked directly. |

```toml
[reset-password]
description  = "Reset the caller's password"
# absent ≡ true — listed by runspec serve/local

[rotate-root-key]
description  = "Rotate the cluster root key"
discoverable = false                           # internal helper — hidden from discovery
```

**Effect on the surfaces:**

- `runspec serve` exposes a runnable as an MCP tool only when its `serve` context
  matches **and** `discoverable` is not `false`. A `discoverable = false` runnable
  is skipped — the way to keep an internal helper off the agent surface even though
  its package is installed.
- `runspec local` still **lists every** runnable (it is the developer's diagnostic
  view), annotating a hidden one as `[hidden]`. Its emitted schemas
  (`--format mcp`/`openai`/`anthropic`) carry `x-discoverable: false` for a hidden
  runnable (omitted otherwise, since `true` is the default).
- Other consumers of the discovery data should treat a hidden runnable the same
  way: runspec-mcp and runspec-tui leave it out, and runspec-console keeps it
  off Forms, its agent and MCP server, and out of its History view.

This is metadata only — it adds no runtime behaviour to the runnable itself.

> **Self-service catalogs.** Marking a runnable as available to a *self-service*
> audience (e.g. a help-desk visitor catalog) is **not** a `runspec.toml` concern
> — it is configured by the consumer of the catalog (e.g. runspec-console), which
> designates the self-service subset in its own git-managed config. The format
> only decides whether a runnable is discoverable at all.

**Validation:** a non-boolean `discoverable` is reported as an error by
`runspec serve` and flagged by `runspec local`.

Default when unspecified: `true`.

### `output`

Declares what the runnable writes to stdout. Used by agent frameworks and the
`runspec serve` layer to interpret the tool's response.

| Value | Meaning |
|---|---|
| `text` | Human-readable output (default) |
| `json` | Structured JSON — agent can parse and act on the response |
| `html` | HTML output (reserved for future UI use) |

Default when unspecified: `"text"`.

### Reserved Names

`config` is the only reserved name. A runnable cannot be named `config`.
`runspec local` will report an error if this name is used.

### Autonomy Levels

| Level | Meaning |
|---|---|
| `autonomous` | Agent runs freely, no confirmation needed |
| `confirm` | Agent must present intent and await human approval |
| `supervised` | Agent runs, human reviews output before it is acted on |
| `manual` | Agent cannot invoke — human only |

Default when unspecified: value of `[config] autonomy-default`, else `"confirm"`.

#### Who autonomy is for

Autonomy is **a contract for agent invocation, not a directive for human users.** A human typing a command at the terminal has already chosen the action by typing it — autonomy doesn't ask them to confirm anything extra. The four levels describe how an MCP host (or other agent runtime) should gate the *agent's* call.

Conforming MCP hosts read the `x-autonomy` field on the tool schema (emitted by `runspec local --format mcp` and by `runspec serve`'s `tools/list`) and gate accordingly:

- `autonomous` — invoke without asking
- `confirm` — present the planned call to the human, wait for approval
- `supervised` — invoke, then show the result before the agent acts on it
- `manual` — never invoke

Hosts that don't honour `x-autonomy` are free to ignore it. The spec does not force any particular gating behaviour at the host layer — that's a quality-of-implementation concern of each MCP host.

#### Tool-side enforcement (recommended for destructive actions)

Because host gating is not universal, runnables that perform destructive actions should also enforce autonomy themselves at runtime. Two properties on the parsed `RunSpec` make this straightforward:

- `args.runspec_agent` — `True` when invoked by an MCP server (`runspec serve` sets `RUNSPEC_AGENT=1` automatically)
- `args.runspec_autonomy` — the effective autonomy after escalation

The recommended pattern for a destructive flag:

```python
if args.delete:
    if args.runspec_agent and args.runspec_autonomy != "autonomous":
        raise SystemExit("✗ --delete requires autonomy='autonomous' for agent invocation")
    # ... proceed
```

This refuses agent invocation of a destructive action unless the runnable's declared autonomy explicitly permits unattended execution. Human invocation is unaffected — a human running the command at a terminal has already chosen the action by passing the flag.

---

## Remote Execution

These fields control how an executor — `runspec serve`, `runspec exec`, `runspec jump`,
an SSH client such as the console — runs the tool: locally or on a remote host, and
as which account.

### `run_as`

The user to run the command as. Four forms are supported, and anywhere a user name
appears it may instead be the reserved value **`"@owner"`**.

**The default is `"@owner"`: a runnable runs as the account that owns its
environment.** The *environment owner* is the owner of the environment root on the
host where the runnable runs — the venv directory (`sys.prefix`) for the Python
pack; the project root (the directory holding its `node_modules`) for the Node pack.
This makes the common deployments work with no configuration:

- **A personal environment** (a venv in a user's home): the person running it *is*
  the owner, so nothing escalates.
- **A shared environment** owned by a service account: every run is escalated to
  that account (`sudo -u svc …`), so the escalation tool's own rules (sudoers)
  decide who may run its tools. No list of users lives in runspec, and every run of
  the environment is made by one account.

Two rules keep the default safe:

- **An executor never escalates when the caller already is the target account**
  (compared by uid). A personal environment never involves `sudo`.
- **`"@owner"` never escalates to root.** If the environment is owned by uid 0 (for
  example a venv created with `sudo python -m venv` or installed into system
  Python), `"@owner"` resolves to no escalation. Running as root always takes an
  explicit `run_as = "root"`.

Resolution order for the value: the runnable's own `run_as` → the `[config]`
`run_as` default → the library default `"@owner"`. An empty string (`""`) at any of
those levels explicitly means **no escalation**: the runnable runs as whoever
invokes it.

**1. Simple string** — same user on all hosts:
```toml
run_as = "oracle"
run_as = "@owner"      # the environment's owner (the default)
run_as = ""            # no escalation: run as the invoking user
```

**2. Environment variable** — resolved by `runspec serve` at startup. Set per host
group via Ansible `group_vars` or equivalent config management:
```toml
run_as = "$ORACLE_RUN_AS"
```

**3. Per-host exact match**:
```toml
run_as.default = "oracle"
run_as.hosts."special-box-01" = "dba"
run_as.hosts."special-box-02" = ""    # no privilege escalation on this host
```

**4. Pattern matching** — Python `re.fullmatch()`, evaluated top to bottom,
first match wins (TOML insertion order preserved):
```toml
run_as.default = "oracle"
run_as.patterns."[lg]pexp[0-9]*" = "orasvc"
run_as.patterns."prod[0-9]*"     = "produser"
```

Forms 3 and 4 may be combined. Resolution order:

1. Exact match in `hosts`
2. First matching pattern in `patterns`
3. `default`
4. No privilege escalation

An empty string (`""`) explicitly means no privilege escalation for that host or
pattern, and `"@owner"` may be used as any `default`, `hosts` or `patterns` value.

Invalid regex patterns cause `runspec serve` to exit with a clear error at startup.
`runspec local` also validates all patterns at startup.

### `become_method`

The privilege escalation method. Default: `"sudo"`.

| Value | Command constructed |
|---|---|
| `sudo` | `sudo {flags} -u {user} {command} {args}` |
| `su` | `su {flags} -c "{command} {args}" {user}` |
| `pbrun` | `pbrun {flags} -u {user} {command} {args}` |
| `dzdo` | `dzdo {flags} -u {user} {command} {args}` |

Note: `su` uses a distinct syntax — the command and args are wrapped in a `-c` string.

### `become_flags`

Optional flags passed to the become method. Example: `"-H"` resets HOME to the target
user's home directory under `sudo`.

### `enforce_run_as`

`run_as` is consumed by the *executor* (`runspec serve`, an SSH client, the console)
**before** the process starts — it wraps the command in `sudo -u {user} …`, so by the
time the runnable's own code runs it is *already* that user. But a runnable can also be
invoked directly — its installed entry point run by hand, from cron, or imported — and
that path never passes through an executor, so nothing escalates and nothing checks who
is running it.

`enforce_run_as` closes that gap. When the runnable calls the library's parser, the
parser resolves `run_as` for the local host and compares the resolved user to the
current process's effective user. The two are compared by **uid** (the target is looked
up with the platform's user database; a numeric `run_as` is taken as a uid directly), so
account aliases sharing a uid are treated as equal.

| Value | Behaviour on mismatch |
|---|---|
| `"error"` (default) | The parser raises / exits non-zero — the runnable does not run. |
| `"warn"` | A warning is written to stderr; the runnable continues. |
| `"off"` | No check is performed. |

The check is a **no-op** when `run_as` resolves to the empty string (no escalation
intended, so no identity to assert), when `"@owner"` resolves to root (see
[`run_as`](#run_as)), or on a platform with no POSIX uid model (e.g. Windows, where
there is nothing to compare).

With the default `run_as = "@owner"`, this means a shared environment's tools
refuse to run directly as anyone but its owner, and the message names the owner
to `sudo -u` to. `enforce_run_as = "warn"` (on the runnable or in `[config]`)
relaxes that to a warning; `run_as = ""` removes the check entirely. Under an executor that escalated correctly
the effective user already matches, so the check passes silently — `enforce_run_as`
only ever fires on the direct/import path (or if escalation silently failed).

Resolution order (most specific wins): the runnable's own `enforce_run_as` → the
`[config]` `enforce_run_as` default → the library default `"error"`. The check is not
performed during pure introspection (loading a spec without parsing argv), so emitting
schemas and listing runnables never depends on who is running them.

### Command building

Executing clients build the final remote command as follows. All interpolated values
must be shell-escaped (Python: `shlex.quote()`).

**Environment values never appear in a command line.** A process's argument vector
is readable by every user on the host (`ps`, `/proc/<pid>/cmdline`), and privilege
escalation tools log it: `sudo` writes each command it runs to syslog by default, and
auditd / EDR agents record `execve` arguments. So the values of the variables an
executor passes to a runnable (the secret-arg channel, credential variables, even
non-secret ones like `RUNSPEC_AGENT`) are written to the process's **standard
input** as an *env frame*, and a fixed *env prelude* reads them, exports them and
`exec`s the runnable. Only variable *names* and the fixed prelude text ever reach an
argument vector.

The prelude runs on one of two interpreters (see *The env prelude*). In the table,
`{prelude-cmd}` is:

- `{venv}/bin/python -c '{py-prelude}'` when `{command}` is an absolute POSIX path
  in a virtual environment's `bin/` directory, `{venv}/bin/{name}`. A venv's `bin/`
  always holds `python`, and it is the interpreter the runnable itself runs on.
  `{venv}` is not a venv when it is `/`, `/usr`, `/usr/local`, `/snap`,
  `/opt/homebrew`, or a directory named `.local`. An executor that can see the path
  (a local run) also requires `{venv}/pyvenv.cfg` to exist.
- `sh -c '{prelude}' sh` otherwise.

In the table, *escalate* means the resolved `run_as` names an account other than the
caller: an empty value, `"@owner"` when the caller is the owner, and `"@owner"`
resolving to root all mean **no escalation**.

| Escalate | env vars | Command constructed | stdin |
|---|---|---|---|
| no | absent | `{command} {args}` | — |
| no | present | `{prelude-cmd} {command} {args}` | env frame |
| yes | absent | `{become_method} {become_flags} -u {user} {command} {args}` | — |
| yes | present | `{become_method} {become_flags} -u {user} {prelude-cmd} {command} {args}` | env frame |

To resolve `"@owner"` for a remote host, the executor learns the owner of the
environment root on that host, for example from the same SSH round-trip it uses
for discovery.

For `su`, the whole `{prelude-cmd} {command} {args}` string is the `-c` argument:
`su {become_flags} -c "{prelude-cmd} {command} {args}" {user}`.

An executor that starts the runnable directly — as a local child process with no
`run_as` and no intermediate shell — may instead set the variables in the child's
environment (e.g. `subprocess.run(..., env=...)`); the environment is readable only
by the process's own user and root. Anything that goes through a shell command
string (an SSH exec channel) or through a privilege-escalation tool must use the
env frame.

#### The env prelude

Both forms read the same env frame from standard input, set each variable in their
own environment and replace themselves with `{command} {args}` (`exec`, searching
`PATH` like the shell). Neither contains a value, so both are safe to log.

**Python prelude** (`{py-prelude}`), used for a command in a venv. It is run as
`{venv}/bin/python -c '{py-prelude}' {command} {args}`, so `sys.argv[1:]` is the
command:

```python
import os,sys
l=bytes()
while 1:
 c=os.read(0,1)
 if c and c!=bytes([10]):
  l+=c;continue
 if l==bytes([46]) or not l and not c:break
 if l:n,s,v=l.partition(bytes([61]));os.environ[n.decode()]=bytes(int(v[i+2:i+5],8) for i in range(0,len(v),5)).decode()
 if not c:break
 l=bytes()
os.execvp(sys.argv[1],sys.argv[1:])
```

It uses only the standard library and reads one byte at a time, so like `read` it
never consumes past the frame. It contains no quote characters, `$` or backticks, so
it embeds verbatim in a single-quoted shell word. Running on the venv's own
interpreter means a sudoers rule that allows the venv's `bin/` (the
`enable-passwordless-sudo` *scoped* grant), or that allows everything except shells
(`ALL, !/bin/sh, !/bin/bash, …`, a common corporate policy), also allows a run that
carries variables.

**Shell prelude** (`{prelude}`), used for any other command. It is this exact text:

```sh
while IFS= read -r l; do [ "$l" = . ] && break; n=${l%%=*}; v=$(printf "%b" "${l#*=}X"); export "$n=${v%X}"; done; exec "$@"
```

`read`, `printf`, `export` and `exec` are builtins in every POSIX `sh` (dash, bash,
ksh, BusyBox ash), so no value ever becomes another process's argument. The trailing
`X` sentinel preserves trailing newlines that command substitution would strip. The
prelude needs nothing on the host beyond `/bin/sh` — no particular runspec version,
no sudoers `env_keep`/`SETENV`, and no `sshd_config` `AcceptEnv`.

The Python prelude likewise needs nothing beyond the venv's own interpreter.

#### The env frame

The executor writes one line per variable, then a line holding a single `.`:

```
NAME=\0ooo\0ooo…
.
```

- `NAME` must match `[A-Za-z_][A-Za-z0-9_]*`; an executor refuses any other name.
- The value is its UTF-8 bytes, **every byte** written as `\0` followed by exactly
  three octal digits (so `a` → `\0141`, newline → `\0012`). The frame is therefore
  plain printable ASCII with no spaces, and a value may contain newlines, quotes,
  `$`, backticks or any other byte. A value containing a NUL byte is refused (an
  environment variable can't hold one).
- After the terminating `.` line the prelude has consumed exactly the frame (`read`
  never reads past a newline on a pipe, and the Python prelude reads one byte at a
  time), so any further bytes the executor writes
  reach the runnable's own standard input unchanged. An executor with nothing else
  to send closes standard input after the frame.

#### Sudoers rules

A run of a venv command that carries env vars runs the venv's own `bin/python`
under the escalation tool, so any rule allowing that venv's `bin/` (the
`enable-passwordless-sudo` *scoped* grant) or allowing everything except shells
covers it. A run of any other command runs `/bin/sh`, so a rule restricted to
specific commands must allow `/bin/sh` for it. `/bin/sh` can run any command, exactly
like the `/usr/bin/env` the previous form required, so allowing it grants nothing
that form did not.

#### Superseded form

Earlier versions of this table placed variables on the command line —
`KEY=val … {command}` without escalation and `… -u {user} env KEY=val … {command}`
with it. That form exposes every value to all users on the host for the whole run
(`sudo` stays running as the parent) and writes it permanently to the `sudo` log.
Executors must not use it.

---

## Argument Definition

Arguments live under `[<name>.args]`.

### Three levels of verbosity

**Bare value** — type and default inferred from the value:
```toml
verbose = false    # flag, default false
workers = 4        # int, default 4
```

**Inline table** — explicit fields on one line:
```toml
input-dir = {type = "path"}
quality   = {default = 85, range = [1, 100]}
format    = {options = ["json", "csv"], default = "json"}
jira-key  = {type = "str", pattern = "[A-Z]+-[0-9]+"}
```

**Full block** — for args needing prose descriptions:
```toml
[<name>.args.quality]
default     = 85
range       = [1, 100]
description = "Controls output quality. Values below 60 rarely useful."
```

### Argument Fields

| Field | Type | Description |
|---|---|---|
| `type` | string | See Types section. Inferred if omitted. |
| `default` | any | Default value. Absence implies `required = true`. |
| `required` | bool | Explicit required flag. Inferred from missing default. |
| `description` | string | Human and agent readable. Doubles as form label. |
| `options` | array | Valid choices. Infers `type = "choice"`. |
| `range` | [min, max] | Valid range for numeric types. |
| `pattern` | string | Regex the value must fully match. `str` type only. |
| `min-length` | int | Minimum string length. `str` type only. |
| `max-length` | int | Maximum string length. `str` type only. |
| `multiple` | bool | Accept multiple values (repeated flag style). |
| `delimiter` | string | Split a single value by this delimiter. |
| `short` | string | Short flag alias e.g. `"-v"`. |
| `env` | string | Environment variable fallback. |
| `deprecated` | string | Deprecation message shown on use. |
| `autonomy` | string | Per-arg autonomy override. Escalates if more restrictive. |
| `ui` | string | Form control hint. Inferred from type if omitted. |
| `hint` | string | Placeholder / hint text for a UI form field. Advisory for `str`/`path`/`password`; never affects parsing. |
| `meta` | table | Developer-defined metadata. Pass-through — runspec never interprets the contents. |
| `position` | int | 1-based positional index. Makes the arg positional (it may still be passed as `--name`). |

### Default Values for Argument Fields

What you actually get when a field is omitted. Two layers of defaulting are
applied: the **loader** normalises the raw TOML into a uniform dict, then
**inference** fills in derived fields (`type`, `required`) based on the
fields that *are* present.

| Field | If omitted, you get | Notes |
|---|---|---|
| `type` | inferred from `default`'s Python type → bool→`flag`, int→`int`, float→`float`, str→`str`; or from `options` → `choice`; final fallback `str` | Inference rules table earlier in this document. |
| `default` | `None` (except `rest` type, which defaults to `[]`) | |
| `required` | `True` if no `default` and `type` is not `flag` or `rest`; `True` if `type = "path"` and no `default`; `False` otherwise | This is what makes `name = {type = "str"}` a required arg with no default. To make it optional, set `required = false` or give it a `default`. |
| `description` | `None` | Recommended in practice — agents lean on this. |
| `options` | `None` | Setting this also infers `type = "choice"`. |
| `range` | `None` | |
| `pattern` | `None` | Anchored — the *whole* value must match (`re.fullmatch` semantics). Applied to `str` values only; ignored for other types. |
| `min-length` | `None` | Applied to `str` values only. |
| `max-length` | `None` | Applied to `str` values only. |
| `multiple` | `False` | |
| `delimiter` | `None` | When set, a single value is `.split(delimiter)`. |
| `short` | `None` | |
| `env` | `None` | When set, the named env var is checked before the spec default. |
| `deprecated` | `None` | Set to a string message; shown on use. |
| `autonomy` | inherits `[config] autonomy-default` (which itself defaults to `"confirm"`) | Per-arg overrides escalate the runnable's effective autonomy if more restrictive. |
| `ui` | derived from `type` at render time (text/number/checkbox/dropdown/etc.) | See "Form Control Inference". |
| `hint` | `None` | Optional UI placeholder text; no effect on parsing or validation. |
| `meta` | `None` | Pass-through — runspec never reads it. |
| `position` | `None` (i.e. the arg is a `--flag`, not a positional) | 1-based when set; a positional is also accepted as `--name`. |

The two non-obvious defaults to remember:

1. **No `default` → `required = true`** for everything except `flag` and `rest` types.
2. **`type = "path"` is always required unless given a `default`** — paths almost never have a sensible fallback.

These defaults are not configurable at the `[config]` level. The single project-wide arg-related setting is `[config] autonomy-default`, which only affects the autonomy inheritance rule above. Keeping the other defaults stable across projects is intentional: it makes any `runspec.toml` predictable to read.

### Uniqueness and Reserved Values

The following constraints are enforced at parse time and cause a clean error
(not silent shadowing) when violated:

- **`short` must be unique within a runnable.** Two args declaring the same
  short flag (e.g. `short = "-v"`) is an error.
- **`-h` is reserved for `--help`.** An arg may not declare `short = "-h"`.
- **`position` must be unique within a runnable.** Two args declaring the
  same `position = N` is an error.
- **At most one `rest` arg per runnable.** Two args with `type = "rest"`
  is an error.

### Positional Arguments

Args with `position = N` are populated from non-flag tokens in declaration
order. Positions are 1-based and must be unique within a runnable.

A positional arg can **also** be passed by name as `--name value` (or
`--name=value`) — the form a programmatic caller building argv from a
`{name: value}` map naturally produces (`runspec serve`, a scheduler, a
console). A positional given by name is not also consumed from the bare
tokens, so the remaining positionals keep their order. Passing the same arg
both ways is an error.

```toml
[deploy.args]
target  = {type = "str", description = "Target host", position = 1}
release = {type = "str", description = "Release tag", position = 2, required = false}
```

```bash
deploy prod v1.2.3       # target=prod, release=v1.2.3
deploy prod              # target=prod, release=None
deploy --target prod --release v1.2.3   # same as `deploy prod v1.2.3`
deploy --release v1.2.3 prod            # target=prod (by position), release by name
```

### Pass-through Arguments (`type = "rest"`)

A single arg with `type = "rest"` captures everything after a literal `--`
token as a list of strings. Used to forward args to a wrapped command.

```toml
[wrap.args]
extra = {type = "rest", description = "Args passed to the wrapped command"}
```

```bash
wrap -- --foo bar --baz       # extra = ["--foo", "bar", "--baz"]
wrap                           # extra = []
```

`rest`-type args are never required and default to `[]`.

---

## Inference Rules

Implementations must apply these rules in order:

| Condition | Inference |
|---|---|
| `default = <integer>` | `type = "int"` |
| `default = <float>` | `type = "float"` |
| `default = <string>` | `type = "str"` |
| `default = true` or `default = false` | `type = "flag"` |
| `options = [...]` present | `type = "choice"` |
| No `default` and no explicit `required = false` | `required = true` |
| `type = "path"` with no default | `required = true` |

---

## Types

| runspec type | Description |
|---|---|
| `str` | Unicode string |
| `password` | Secret string. Coerces like `str` (honours `pattern`/`min-length`/`max-length`), but is masked in UIs, **refused on the command line**, omitted from emitted agent schemas, and redacted from logs. See *Secret arguments*. |
| `int` | Integer |
| `float` | Floating point number |
| `bool` | Boolean (true/false) |
| `flag` | Boolean switch — presence means true |
| `path` | File system path |
| `choice` | One of a declared set of options |
| `rest` | List of strings captured after a literal `--` separator. At most one per runnable. |

Language packs may register additional types via the type registry.

---

## Secret Arguments

The `password` type marks an argument whose value is a secret (a password,
token, or API key). It behaves like `str` for coercion and validation but is
handled differently everywhere a value could leak:

- **Refused on the command line.** Passing a `password` arg as `--name value`
  (or `--name=value`, or via its `short` flag) is a hard error. A secret on the
  command line lands in shell history and the process table (`ps`), so runspec
  refuses it outright. This is the meaning of "can't be done on the command
  line".
- **Resolved from the environment.** A `password` value comes from the normal
  resolution chain *minus* the CLI tier: the automatic
  `RUNSPEC_<RUNNABLE>_ARG_<NAME>` variable, a declared `env` alias, or the
  `.runspec_env` file (see *Value Resolution Order*).
- **Interactive prompt (fallback).** When a required `password` arg is still
  unresolved and the process is attached to a terminal (and not an agent
  invocation), implementations should prompt for it without echoing input.
  Non-interactive and agent runs do not prompt — they rely on the environment.
- **Omitted from agent schemas.** `password` args are excluded from emitted
  MCP / agent tool schemas (`inputSchema`), so an agent is never asked to
  supply a secret. The operator provides it through the environment.
- **Kept out of logs.** A `password` value must not appear in the run-summary
  audit record (`invocation_args`) or other structured log output.
- **Never on an executor's command line.** An executor (the console, an SSH
  client, `runspec serve`/`exec` escalating with `run_as`) passes the secret
  channel through the process environment or an *env frame* on standard input,
  never as `KEY=val` or `env KEY=val` in a command — see *Remote Execution →
  Command building*.

```toml
[deploy.args]
db-password = {type = "password", description = "Database password"}
api-token   = {type = "password", env = "DEPLOY_API_TOKEN", required = false}
```

```bash
deploy --db-password hunter2          # ✗  refused — passwords can't go on the CLI
RUNSPEC_DEPLOY_ARG_DB_PASSWORD=… deploy   # ok — supplied via the environment
deploy                                 # interactive: prompts "db-password:" (no echo)
```

A UI that builds a form for a runnable (e.g. runspec-console) renders a
`password` arg as a masked field and supplies the entered value to the
execution as the `RUNSPEC_<RUNNABLE>_ARG_<NAME>` environment variable — never as
a command-line argument — so the same no-CLI guarantee holds for form-driven
runs.

---

## Value Resolution Order

For every argument, implementations must resolve the value in this order:

1. Explicit CLI argument — highest priority
2. `RUNSPEC_<RUNNABLE>_ARG_<ARGNAME>` environment variable (automatic for every arg)
3. `env` aliases (developer-declared list, checked in order)
4. `.runspec_env` file value (loaded at parse time — does not overwrite existing env)
5. Default from spec
6. Error: required — if nothing above matched and `required = true`

Implementations must track the source of each resolved value and expose it via
an `Arg.source` field (or equivalent). Valid source values:

| Source | Meaning |
|---|---|
| `"cli"` | Provided explicitly on the command line |
| `"env"` | Resolved from a system environment variable (auto `RUNSPEC_*` or a declared `env` alias) |
| `"runspec_env"` | Resolved from the `.runspec_env` file |
| `"spec_default"` | Came from `default = ...` in `runspec.toml` |
| `"not_set"` | No value — arg is optional and was left absent |

The `run_summary` audit record includes both `invocation_args` (plain values) and
`arg_sources` (provenance strings) so the full invocation context is
reconstructable from the log alone. (`"args"` is a reserved `LogRecord` field and
cannot be used as an `extra` key — hence `invocation_args`.)

---

## Runtime Environment Variables

When `runspec serve` executes a tool locally, or when `runspec jump` runs a tool
on a jump box, all resolved argument values are injected as environment variables
before the process starts. This makes any runnable language — bash, Python, Node, or anything
else — work without a language-specific library.

### Naming convention

Per-arg variables include the runnable name to prevent clashes when multiple
runnables share the same argument name:

```
RUNSPEC_<RUNNABLE_UPPERCASED>_ARG_<ARG_NAME_UPPERCASED>
```

Hyphens and underscores in both the runnable name and arg name become underscores.
Examples for a runnable named `backup-logs`:

| Arg name | Environment variable |
|---|---|
| `env` | `RUNSPEC_BACKUP_LOGS_ARG_ENV` |
| `dry-run` | `RUNSPEC_BACKUP_LOGS_ARG_DRY_RUN` |
| `input_file` | `RUNSPEC_BACKUP_LOGS_ARG_INPUT_FILE` |
| `max-retries` | `RUNSPEC_BACKUP_LOGS_ARG_MAX_RETRIES` |

These variables serve two purposes:
- **Input (user-settable):** Set `RUNSPEC_BACKUP_LOGS_ARG_QUALITY=95` in your shell to make that your persistent default whenever no CLI arg is passed.
- **Output (runtime-injected):** `runspec serve` and `runspec jump` inject resolved values into the subprocess environment so bash/node/any-language scripts can read them directly without a library.

The `env` field on an arg declares additional aliases checked after `RUNSPEC_<RUNNABLE>_ARG_<NAME>`. Accepts a string or list of strings:

```toml
[compress.args.quality]
env = ["CI_QUALITY", "ANSIBLE_QUALITY"]  # also checked if RUNSPEC_COMPRESS_ARG_QUALITY unset
```

### Serialisation

| Arg type | Value | Env var |
|---|---|---|
| `flag` | not passed (default false) | `0` |
| `flag` | passed | `1` |
| `bool` | `false` | `0` |
| `bool` | `true` | `1` |
| `str`, `int`, `float`, `path`, `choice` | any value | natural string form |
| `multiple = true` | list of values | newline-delimited string |

The same formats are accepted on the input side — a user setting `RUNSPEC_BACKUP_LOGS_ARG_DRY_RUN=1` or `RUNSPEC_BACKUP_LOGS_ARG_DRY_RUN=true` both coerce correctly to a flag.

Defaults from the spec are always set, even when the caller did not pass the arg
explicitly. A required arg that was not provided is not set (this is a validation
error — the tool should never be reached).

### Reserved variables

| Variable | Set when | Purpose |
|---|---|---|
| `RUNSPEC_AGENT=1` | always (every `runspec serve` invocation) | The runnable can branch on agent vs. human invocation. |
| `RUNSPEC_LABEL=<label>` | by the launcher (`runspec serve` sets `mcp`) | Recorded as the run's label (see "Run labels"); `cli` when unset. |
| `RUNSPEC_CONFIG=/abs/path/to/runspec.toml` | `runspec serve` subprocesses the runnable | Points `parse()` at the spec file directly, bypassing the lookup cascade. Without this, the subprocess inherits SSH's `$HOME` cwd and the cwd-walk fallback would fail. |

`parse()` consults `RUNSPEC_CONFIG` after an explicit `config_path=` arg, then
walks up from the caller's package directory, then from cwd. So the
resolution order in language packs is:

1. Explicit `config_path` passed to `parse()`
2. `RUNSPEC_CONFIG` environment variable
3. Walk up from the caller's `__file__` directory (the package directory
   where `runspec.toml` was shipped — found via call-stack introspection).
   This makes installed entry points work from any working directory.
4. Walk up from cwd looking for `runspec.toml` (ad-hoc usage and back-compat)
5. Error

### Example

```toml
[backup-logs]
description = "Back up application logs"
autonomy    = "confirm"

[backup-logs.args]
env     = {type = "choice", options = ["prod", "staging"]}
days    = {type = "int", default = 7}
dry-run = {type = "flag", default = false}
```

```bash
#!/bin/bash
# env vars are already set and validated — no parsing needed
if [ "$RUNSPEC_BACKUP_LOGS_ARG_DRY_RUN" = "1" ]; then
    echo "Would back up $RUNSPEC_BACKUP_LOGS_ARG_DAYS days of $RUNSPEC_BACKUP_LOGS_ARG_ENV logs"
    exit 0
fi
aws s3 sync "/var/log/app/$RUNSPEC_BACKUP_LOGS_ARG_ENV" "s3://logs-$RUNSPEC_BACKUP_LOGS_ARG_ENV" \
    --delete --include "*.log"
```

---

## .runspec_env File

A `KEY=VALUE` dotenv file loaded at parse time and merged into `os.environ`
(existing env vars are never overwritten). This is a deployment-time mechanism
for injecting environment-specific values (API keys, endpoint URLs) into a venv
without modifying the OS environment or using shell profile scripts.

### Path resolution (four tiers, first match wins)

1. `RUNSPEC_ENV_FILE` environment variable — absolute escape hatch for testing
2. Per-runnable `runspec_env` key in `runspec.toml`
3. `[config] runspec_env` key in `runspec.toml`
4. `{sys.prefix}/.runspec_env` — default; silent skip if absent

Relative paths in tiers 2–3 resolve from `sys.prefix`.

### File format

Standard `KEY=VALUE` pairs. Comments (`#`) and blank lines are ignored. Single
and double quotes strip the surrounding character. Lines without `=` are ignored.

```
# .runspec_env
MY_API_KEY=abc123
DB_URL=postgres://localhost/mydb
QUOTED_VALUE="hello world"
```

### Access

```python
args = parse()
env = args.get_runspec_env()   # SimpleNamespace; keys are lowercased
print(env.my_api_key)          # "abc123"
```

### Inspect with CLI

```
runspec env                  # show default file path and contents
runspec env <runnable>       # show the file resolved for a specific runnable
```

### `runspec_` namespace reservation

Arg names starting with `runspec_` or `runspec-` are **reserved** for the
framework. Attempting to declare such an arg raises a hard error at parse time.
This reservation protects the `RunSpec` metadata properties (`runspec_runnable`,
`runspec_autonomy`, `runspec_agent`, etc.) from being shadowed by user-defined args.

---

## Denylist File

A venv-scoped kill switch that disables runnables without uninstalling them or
editing their `runspec.toml`. It is a **separate file** from `runspec.toml`
(not part of a runnable's spec) read on every `parse()` — so it applies
uniformly to direct CLI runs, installed entry points, and agent runs via
`runspec serve`. A disabled runnable is refused with a uniform error and a
non-zero exit.

### Path resolution (two tiers, first match wins)

1. `RUNSPEC_DENYLIST_FILE` environment variable — absolute escape hatch for testing
2. `{sys.prefix}/runspec_denylist.toml` — default; silent skip if absent

### File format

```toml
[denylist]
mode = "enforce"          # "enforce" (default) | "warn" | "off"
# disabled = true         # fast path: deny everything

[[denylist.rule]]
runnables = ["deploy"]    # omitted ⇒ ["*"] (all runnables)
users = ["svc-batch"]     # omitted ⇒ ["*"] (all users)
hosts = ["prod-*"]        # omitted ⇒ ["*"] (all hosts)
reason = "change freeze"  # surfaced in the refusal message
```

### Matching semantics

- A run is denied when **any** rule matches. Within a rule, the runnable name
  AND the effective username AND the hostname must each match at least one glob
  in their list; multiple rules OR together.
- `runnables` / `users` / `hosts` are shell-style globs (`fnmatch`, `*`
  wildcard). An omitted (or empty) dimension means "all".
- `disabled = true` is a fast path equivalent to one all-`*` rule.
- The **effective** user is matched (`pwd.getpwuid(os.geteuid())`, falling back
  to `getpass.getuser()` on Windows) — the post-`run_as` identity the runnable
  actually executes as. Hostname is `socket.gethostname()`, the same source the
  `run_as` gate uses.

### Enforcement mode

`mode` lives in the denylist file and is **authoritative** — there is no
per-runnable opt-out, so a runnable cannot exempt itself from an operator's
denylist (unlike `enforce_run_as`, which a runnable declares).

| `mode` | Behaviour |
|---|---|
| `enforce` (default) | Refuse the run; raise and exit non-zero. |
| `warn` | Write a warning to stderr and continue. |
| `off` | No enforcement (rules retained but inert). |

### Exemptions

- `--help` is handled before the gate, so a disabled runnable's help is still
  readable.
- Introspection (`load_spec`, used by `runspec local` / `runspec stubs`) is
  never blocked.
- The reserved name `runspec` (the core CLI dogfooding its own spec) is never
  denied, so `runspec deny clear` / `runspec serve` / `runspec local` keep
  working even under a deny-everything rule — the guaranteed re-enable path.

### Manage with the CLI

```
runspec deny add --runnable deploy [--user G] [--host G] [--reason T]
runspec deny all [--reason T]      # disable everything
runspec deny list                  # show mode + rules
runspec deny mode enforce|warn|off
runspec deny remove N              # remove rule N (1-based, as numbered by `deny list`)
runspec deny remove -r G [-u G] [-H G]  # remove every rule whose lists equal these exactly
runspec deny enable                # undo `deny all` (unset `disabled`); rules are kept
runspec deny clear                 # remove the file — re-enable everything
runspec deny list --json           # structured status: {path, present, mode, disabled, reason, rules, hostname}
```

`remove` by match compares each dimension as a set, with an omitted dimension
meaning `["*"]` (the same normalisation `add` applies), so the arguments that
added a rule also remove it. `hostname` in the JSON is the machine's
`socket.gethostname()` — the value a rule's `hosts` globs are matched against —
so a tool can check a rule will match before writing it.

`runspec deny` is a core subcommand (not a runnable), so it is never subject to
the denylist it edits.

---

## Installed-Code Integrity

Before a runnable's code is trusted with a run, `parse()` checks that the
installed package still matches what was installed: every file listed in the
package's `RECORD` with a hash is hashed again and compared. A file that was
edited or deleted after installation marks the run as **modified**. The check
is a warning and a record, never a refusal.

It is tamper-*evident*, not tamper-proof. An account that can edit an installed
file can also edit `RECORD`, edit runspec itself, or edit the runnable so it
never calls `parse()` — and module code runs at import, before `parse()` is
reached. The check catches edits made on a host and never committed back (a
quick fix in `site-packages`), not someone set on hiding a change. Code that
must not be changed by the account that runs it belongs to a different account
(a root-owned venv, or a `run_as` that does not own the venv).

### What is checked

- The **distribution that owns the runnable's `runspec.toml`** (the installed
  file is located among the distribution's files), plus each distribution it
  requires directly (`Requires-Dist`, skipping requirements that only apply to
  an extra, and requirements that are not installed). For a typical wrapper this
  is the runnable package, its `-core` package and runspec itself.
- Every file in those distributions' `RECORD` that carries a hash: the `.py`
  sources, `runspec.toml` and any other data files, and the console scripts.
  Files pip records without a hash (byte-code under `__pycache__`, `RECORD`
  itself, `INSTALLER`, `direct_url.json`) are not checked, and files *added* to
  a package directory are not looked for.
- A `runspec.toml` that is not inside an installed distribution (a project
  checkout, a script next to its spec) is not checked.

### Where the package came from

PEP 610's `direct_url.json` in the distribution's `.dist-info` says how it was
installed:

| `direct_url.json` | Source | Checked | Recorded as |
|---|---|---|---|
| absent | `index` (PyPI, a private index) | yes | — |
| `dir_info.editable = true` | `editable` (`pip install -e`) | no | — |
| `dir_info` | `folder` (`pip install ./pkg`) | yes | `local` |
| `archive_info` | `archive` (a wheel or sdist file or URL) | yes | `local` |
| `vcs_info` | `vcs` (`pip install git+…`) | yes | `local` |

An editable install is never checked, so a developer's working tree is never
reported. A distribution with no hashed `RECORD` entries (an old egg install)
is reported as `unverifiable` and not flagged.

### Result

The run's **integrity** is one word, empty when there is nothing to report:

| `integrity` | Meaning |
|---|---|
| `modified` | At least one checked file differs from its `RECORD` hash or is missing |
| `local` | Nothing differs, but a checked distribution was installed from a folder, a file or a VCS rather than a package index (code that may never have been committed or published) |
| (empty) | Everything checked matches and came from an index, or nothing was checked |

It is recorded as the `integrity` field of `run_start` and `run_summary` and
the runs index's `integrity` column. A `modified` run also carries
`integrity_files`: up to 20 `"<distribution>: <path>"` entries.

On `modified`, `parse()` also:

1. writes one warning to stderr naming the changed files, on every run; and
2. records the audit event `runnable.modified` (outcome `detected`, target
   `{"distribution": …, "version": …}`, detail `{"files": …, "source": …}`) for
   each modified distribution — once per distinct set of changes, so a
   runnable that runs every minute does not repeat it. The last set seen is kept
   in `{installation_root}/logs/.integrity-state.json`; an account that cannot
   write it records the event on every run.

`RUNSPEC_INTEGRITY=off` turns the check off (test suites, or a host where a
known local change is accepted). There is no `runspec.toml` switch: a package
cannot turn off its own check. The core `runspec` CLI is not checked on its own
runs; it is checked as a dependency of every runnable.

### `runspec verify`

```
runspec verify                 # every installed runnable package + its direct requirements
runspec verify runspec-linux   # one distribution (or a runnable name)
runspec verify --all           # every distribution in the environment
runspec verify --json          # structured result
```

Prints each distribution's version, source and status (`ok`, `modified`,
`skipped` for editable, `unverifiable`), and the changed or missing files.
Exits `1` when anything is modified, else `0`. It records nothing.

### Node pack

npm keeps no per-file hashes (only one hash of the downloaded tarball), so the
Node pack gets its reference hashes another way. The result, the run record
fields, the warning and `RUNSPEC_INTEGRITY=off` are the same as above; what
differs is what is checked and where the known-good hashes come from.

**What is checked.** A Node runnable set is a project folder: `runspec.toml`,
`package.json`, its scripts, and `node_modules`. `parse()` checks:

- **The project** — the folder holding the nearest `package.json` above the
  runnable's `runspec.toml`, outside `node_modules`.
- **Each direct dependency** — every `dependencies` and `optionalDependencies`
  entry of that `package.json` installed at `node_modules/<name>`.

Nothing is checked when the project folder is inside a git work tree (a `.git`
directory in it or above it: a developer's checkout) or has no `package.json`.

**Where a package came from.** `node_modules/.package-lock.json` (written by npm
7+) records each dependency's `resolved` URL:

| Install | Source | Checked | Recorded as |
|---|---|---|---|
| a registry URL, or no lock entry | `index` | yes | — |
| a symlink (`npm link`, `npm install ../dir`, a workspace) | `linked` | no | — |
| `file:…tgz` | `archive` | yes | `local` |
| `git+…`, `github:…` and other git specs | `vcs` | yes | `local` |

The project itself is always `index` (it was unpacked or copied into place; how
is not recorded).

**Known-good hashes.** For each package, in order:

1. **A packed hash list** — `runspec-files.json` in the package folder:

   ```json
   {"version": 1, "algorithm": "sha256", "name": "runspec-webops", "packageVersion": "0.2.0",
    "files": {"dist/cert-check.js": "<hex sha-256>", "runspec.toml": "<hex>", "package.json": "<hex>"}}
   ```

   `runspec manifest` writes it from the file list `npm pack` would publish,
   so a publisher adds it with one `prepack` script (after the build):
   `"prepack": "npm run build && runspec manifest"`. Every listed file is
   checked; a file the list doesn't name is not.
2. **A first-run snapshot** — for a package without a list, the first run that
   finds no snapshot for that package's name and version hashes its files and
   stores them in `{project}/logs/.integrity-baseline.json`; later runs compare
   against it. The files are every regular file in the package folder except
   symlinks, dot-files and dot-directories, nested `node_modules`, and, for the
   project, `logs/`, `bin/` (the shims `runspec bin` writes) and
   `package-lock.json`. A package of more than 5000 files is `unverifiable`. An
   edit made before the first run is part of the snapshot and is never
   reported; `runspec verify --snapshot` takes the snapshot again after a
   reinstall. An account that cannot write the file checks nothing until one
   that can has run.

A list is the reference a publisher controls; a snapshot only says "unchanged
since this host first ran it". Both are tamper-evident only, like `RECORD`.

**Result.** `integrity` / `integrity_files` are written on `run_summary` when
`[config.logging]` is on (the Node pack has no `run_start` or runs index). The
stderr warning is written on every `modified` run, and the `runnable.modified`
audit event is recorded as above (target `{"package": …, "version": …}`), once
per distinct set of changes, remembered in `{project}/logs/.integrity-state.json`.

**`runspec verify`** (Node):

```
runspec verify                 # the project + its direct dependencies
runspec verify runspec-node    # one package (or a runnable name: the project)
runspec verify --all           # every package in node_modules
runspec verify --json          # structured result
runspec verify --snapshot      # (re)take the snapshot of packages without a list
```

Prints each package's version, source, baseline (`list` / `snapshot` / `none`)
and status. Exits `1` when anything is modified, else `0`. Without `--snapshot`
it records nothing, so a package with no list and no snapshot yet shows
`unverifiable`.

---

## Mock Playback

A runnable can be run in **mock mode**: it accepts and validates its arguments
exactly as usual, then prints a previously recorded response and exits with the
recorded exit code. **None of the runnable's own code runs** — nothing is
contacted, written or changed. Mock mode exists so agents, evals, demos and
tests can exercise a runnable with realistic output and no credentials or side
effects.

Recorded responses live in **mock files**, separate from `runspec.toml`.

### Turning it on

| Variable | Value | Effect |
|---|---|---|
| `RUNSPEC_MOCK` | `1` / `true` / `yes` / `on` | Mock mode, best match (see Matching). |
| `RUNSPEC_MOCK` | `strict` | Mock mode, exact match only. |
| `RUNSPEC_MOCK` | unset, empty, anything else | Off — a normal run. |
| `RUNSPEC_MOCK_DIR` | a directory | An extra, highest-priority place to look for mock files. |

An executor that has `RUNSPEC_MOCK` in its own environment passes it on to
every run it starts. A run that would lose the environment (`run_as`
escalation, a remote host) gets it in the env frame, as `RUNSPEC_AGENT` does.
`RUNSPEC_MOCK_DIR` names a local directory, so it is passed on only to runs on
the same machine. The core `runspec` CLI is never mocked.

Mock mode **never falls through to a real run.** When no recorded response
matches, the run fails (see No match).

### Where mock files are found

A runnable's mock file is named `<runnable>.json`. `parse()` reads every one of
these that exists, highest priority first:

1. `$RUNSPEC_MOCK_DIR/<runnable>.json`
2. `{sys.prefix}/runspec_mocks/<runnable>.json` — the venv overlay: mocks
   collected on this machine, never part of a package
3. `<directory of runspec.toml>/mocks/<runnable>.json` — shipped with the
   package (a build backend that includes the package directory includes it)

### File format

```json
{
  "runspec_mock": 1,
  "runnable": "jira",
  "entries": [
    {
      "command": ["get-issue"],
      "args": {"key": "OPS-1"},
      "exit_code": 0,
      "stdout": {"key": "OPS-1", "summary": "Disk full on web-01", "status": "Open"},
      "recorded": "2026-10-18T09:00:00Z",
      "source": "record",
      "note": "compact view"
    },
    {
      "command": ["get-issue"],
      "args": {},
      "exit_code": 1,
      "stdout": {"error": "Issue does not exist", "kind": "JiraError"}
    }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `runspec_mock` | yes | Format version. Currently `1`. |
| `runnable` | yes | The runnable the file belongs to. |
| `entries` | yes | Recorded responses. |
| `entries[].command` | no (default `[]`) | Subcommand path, outermost first. `[]` for a runnable with no subcommands. |
| `entries[].args` | no (default `{}`) | The arguments this response answers (see Matching). Never contains a `password` arg. |
| `entries[].exit_code` | no (default `0`) | Exit code to exit with. |
| `entries[].stdout` | no (default `""`) | A string is written verbatim. Any other JSON value is written as JSON (2-space indent) followed by a newline. |
| `entries[].stderr` | no | Written verbatim to stderr. |
| `entries[].recorded`, `source`, `note` | no | Informational: when and how the entry was made (`source` is `"record"` for `runspec mock record`, anything else for hand-written entries). |

Implementations must ignore unknown fields.

### Matching

The **call's args** are the arguments whose value came from the command line or
the environment (the provenance sources `cli`, `env` and `runspec_env`),
excluding `password` args and the auto-injected `debug` flag. Defaults are not
part of the call's args, so changing a default does not invalidate recordings.

Arg names are compared after replacing `-` with `_`. Values are compared after
normalising: a boolean is `true`/`false`, a number or string is its string
form, and a list is compared element by element. So `5` and `"5"` match, but `5`
and `5.0` do not.

Among the entries whose `command` equals the call's subcommand path:

- **Best match** (`RUNSPEC_MOCK=1`): an entry matches when every arg in its
  `args` is in the call's args with an equal value. The matching entry with the
  most `args` wins; ties go to the higher-priority file, then to the earlier
  entry in the file. An entry with `args = {}` is therefore the command's
  fallback answer.
- **Strict** (`RUNSPEC_MOCK=strict`): an entry matches only when its `args` and
  the call's args have exactly the same names and values.

### What still happens in mock mode

`--help`, the denylist, `require-command`, argv parsing, value resolution,
both validation passes, coercion and autonomy all run exactly as in a normal
run, so a bad argument fails with the usual error. Differences:

- The `run_as` identity check is skipped (nothing runs as anyone).
- Secrets are not prompted for, and a required `password` arg that has no value
  is treated as present.
- After logging is configured, the matched response is written and the process
  exits. The run record (`run_summary`) carries `"mock": true` and the recorded
  exit code.
- When stderr is a terminal, one line on stderr says the response is a mock.
  Nothing extra is written when stderr is a pipe or a log.

### No match

When no entry matches, the run exits with code **69** and writes one JSON
object to stdout:

```json
{
  "error": "no mock response recorded for these arguments",
  "kind": "MockNotFound",
  "runnable": "jira",
  "command": ["get-issue"],
  "args": {"key": "OPS-2"},
  "recorded": [{"key": "OPS-1"}]
}
```

`recorded` lists the `args` of every entry for that command, so a caller (or an
agent) can see what is available.

### Manage with the CLI

```
runspec mock record <runnable> -- [command…] [args…]   # run it for real, save the response
runspec mock list [<runnable>]                          # what is recorded, where, how big
runspec mock play <runnable> -- [command…] [args…]     # the same as RUNSPEC_MOCK=1 <runnable> …
runspec mock check [<runnable>]                         # every entry still valid for the spec?
```

`record` runs the installed runnable with `RUNSPEC_AGENT=1` (so the response is
the agent-shaped output), captures stdout, stderr and the exit code, and saves
an entry keyed on the args given on the command line. An existing entry with the
same command and args is replaced. It writes to the package's `mocks/` folder
when the package is installed from a source checkout (an editable install),
otherwise to the venv overlay; `--to package|venv|<dir>` overrides. `--redact`
(a regular expression, repeatable) replaces matches in the saved output with
`«redacted»`.

`record` learns the key from the runnable itself: it runs it with
`RUNSPEC_MOCK_RECORD=<file>`, and `parse()` — once the arguments are accepted —
writes `{"command": [...], "args": {...}}` (command-line args only, same
exclusions as matching) to that file, removes the variable from the
environment, and carries on with the normal run. A language pack implements
this handoff so its runnables can be recorded.

`check` exits non-zero when an entry names a subcommand or arg the spec no
longer has, a value fails validation, an entry keys on a `password` arg, or a
response contains something that looks like a credential.

Mock files are shipped in packages. Responses recorded from real systems must
be scrubbed before they are committed; keep mocks made from private data in the
venv overlay or in a private package.

---

## Audit Events

Every run is recorded by the [run audit](#logging). **Audit events** record the
important moments *inside* a run (a refund issued, a host rebooted) or outside
one (a deny rule added). A language pack exposes one call — Python
`runspec.audit(event, outcome, *, target, detail, strict)`, Node
`audit(event, outcome, { target, detail, strict })` — and the core CLI exposes
the same as `runspec audit record`.

### Record

| Field | Type | Description |
|---|---|---|
| `v` | int | Record version (`1`) |
| `ts` | string | UTC ISO-8601 timestamp |
| `id` | string | UUID4 of this event |
| `event` | string | Dotted lowercase name chosen by the caller (`^[a-z0-9][a-z0-9_.-]{0,99}$`), e.g. `payment.refund` |
| `outcome` | string | Short lowercase word (`started`, `succeeded`, `failed`, `denied`…) or empty |
| `actor` / `uid` | string / int | Effective OS user that wrote the event |
| `host`, `pid`, `venv` | | Where it was written |
| `runnable`, `command`, `run_id` | string | The run it belongs to (empty outside a run) |
| `agent`, `trigger` | bool / string | Provenance, as in the run summary |
| `target` | object | What was acted on |
| `detail` | object | Extra context |
| `prev`, `hash` | string | Ledger chain (see below) |

Keys matching the secret-key pattern (`password`, `token`, `api_key`,
`secret`…) and secret-looking values inside `target`/`detail` are redacted, as in
log records. An invalid `event` name is a programming error (Python
`ValueError`); otherwise recording never raises, unless `strict` is set and no
sink took the event (Python `AuditError`).

### Sinks

1. **The venv ledger** — `{installation_root}/logs/audit.jsonl`. Opened in
   append mode, never rotated, truncated, pruned or compacted. Each line is the
   record plus `prev` (the previous line's `hash`, or `sha256:` + 64 zeros for
   the first line) and `hash` = `sha256:` + hex SHA-256 of the record *including*
   `prev` but excluding `hash`, serialised as JSON with sorted keys and
   `(",", ":")` separators (see *Canonical form* below). Editing, deleting,
inserting or reordering a line
   breaks the chain; `runspec audit verify` reports the first bad line. Only the
   venv owner (or root) appends. Another account in a shared venv writes its
   events into its own per-run [inbox](#logging) file; the owner's sweep appends
   them to the ledger with `actor` replaced by the file's kernel-attested owner
   (the claimed actor kept as `claimed_actor`) and `via = "inbox"`. With no
   writable venv `logs/`, the ledger is `{home}/logs/audit.jsonl`.
2. **The OS log** — POSIX: syslog (ident `runspec`, facility from
   `[config.audit] facility`, default `auth`, priority `notice`); Windows: the
   Application event log (source `runspec`, event id 200). The message is
   `runspec-audit ` followed by the record as JSON, including the ledger `hash`
   when there is one, so truncating the ledger is detectable against the OS log.
3. **The run's log** — inside a run, a file-only record on the `runspec.audit`
   logger with `extra.event = "audit"` and the record under `extra.audit`.

| `[config.audit]` field | Type | Default | Description |
|---|---|---|---|
| `syslog` | bool | `true` | Also write events to the OS log |
| `facility` | string | `"auth"` | Syslog facility (POSIX) |

There is no switch to turn the ledger off.

### Canonical form

The hash input is the record (without `hash`) as UTF-8 JSON, written the way
Python's `json.dumps(record, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)` writes it, so a ledger written by one language pack
verifies in another:

- Object keys sorted by Unicode code point, no whitespace.
- Strings: `"` and `\` escaped with a backslash; `\b \f \n \r \t` as
  those escapes; any other character below U+0020 as `\u00xx` (lowercase hex);
  everything else, non-ASCII included, written as is.
- Integers as plain digits. Non-integer numbers as Python's `repr(float)`: the
  shortest digits that round-trip, written as `0.0001`, `1.5`, `1e-05`, `1e+16`
  (exponent form when the decimal exponent is below −4 or 16 and above, with a
  sign and at least two exponent digits), and a whole-valued float keeps `.0`.
  `NaN`, `Infinity` and `-Infinity` as those words. A verifier keeps whether a
  number was written as an integer or a float, so `1` and `1.0` hash
  differently, as in Python.

A pack that can't tell integers from floats (JavaScript) writes every whole
number as an integer and every other number in the float form above.

### Node pack

`audit()` from `runspec-node` writes the same record to the same three sinks,
with these differences:

- **Ledger** — `{project}/logs/audit.jsonl`, where `{project}` is the folder
  holding the nearest `package.json` above the runnable's `runspec.toml`
  (recorded as `venv`). The Node pack has no inbox: an account that is neither
  root nor the owner of `{project}/logs` (or of `{project}`, before `logs/`
  exists) writes `{home}/logs/audit.jsonl` instead. Appends are serialised with a
  lock file, `audit.jsonl.lock`, created exclusively next to the ledger and
  holding the writer's pid; a lock older than 30 seconds or whose pid is gone is
  taken over. A writer that can't get the lock in 5 seconds skips the ledger.
- **OS log** — POSIX: the `logger` command (`logger -t runspec[<pid>] -p
  <facility>.notice`), when `/dev/log` (or `/var/run/syslog`, `/var/run/log`)
  exists; Windows: `eventcreate`, as above.
- **Run log** — only when the runnable has `[config.logging]` (logging is
  opt-in in the Node pack).
- The context (`runnable`, `command`, `run_id`, `agent`, `trigger`) is set by
  `parse()` whether or not logging is on; `run_id` is empty without logging.

`RUNSPEC_AUDIT=off` turns `audit()` off in the Node pack too (tests and CI only).

### Control-plane events

The core CLI records its own state-changing verbs: `denylist.change`
(`runspec deny` add/all/remove/enable/clear/mode), `logs.share`, `logs.prune`
and `logs.compact`. The Node CLI records `logs.prune` and `logs.compact` (it has
no `deny` or `logs share`).

### Integrity

Local records are tamper-*evident*, not tamper-proof: root or a local
administrator can always erase them. An immutable record has to leave the
machine — ship the OS log (syslog/journald) to a central system.

## Groups

Groups define relationships between arguments.
They live under `[<name>.groups.<group-name>]`.

### Group Types

**Mutually exclusive** — at most one arg from the group may be provided:
```toml
[<name>.groups.output]
exclusive = true
args      = ["format", "raw"]
```

**Mutually inclusive** — if any arg is provided, all must be:
```toml
[<name>.groups.auth]
inclusive = true
args      = ["username", "password"]
```

**At least one** — one or more must be provided:
```toml
[<name>.groups.input]
at-least-one = true
args         = ["input-file", "input-dir", "input-glob"]
```

**Exactly one** — strictly one must be provided:
```toml
[<name>.groups.mode]
exactly-one = true
args        = ["fast", "balanced", "quality"]
```

**Conditional** — if one arg is provided, others become required:
```toml
[<name>.groups.upload]
if       = "upload"
requires = ["bucket", "region"]
```

### Escalation Rule

The most restrictive autonomy level wins across all provided args.
An agent can never escalate beyond what the spec allows.

---

## Subcommands

Subcommands live under `[<name>.commands.<subcommand>]`.
Each subcommand has its own `args`, `groups`, `autonomy`, and `description`.

```toml
[pipeline.commands.run]
description = "Run the pipeline"
autonomy    = "confirm"

[pipeline.commands.run.args]
input = {type = "path"}

[pipeline.commands.validate]
description = "Validate without running"
autonomy    = "autonomous"
```

### Global (inherited) arguments

The runnable's own top-level `args` are **global arguments**: every subcommand
inherits them. The effective argument set for a subcommand invocation is the
runnable's top-level args merged with the args declared at each level along the
resolved command path. When a subcommand declares an arg with the same name as
an inherited one, the subcommand's definition wins (child overrides parent).

A required global arg is required regardless of which subcommand is invoked.

```toml
[deploy]
[deploy.args]
region = {type = "choice", options = ["us", "eu"], required = true}
env    = {required = true}

[deploy.commands.show]
[deploy.commands.show.args]
symbol = {required = true}
```

```
deploy --region eu --env qa show --symbol ABC123
```

**Ordering.** Global flags must appear **before** the command token, matching
`git`/`docker`/`kubectl` and Python's `argparse` subparsers
(`deploy --region eu show`, not `deploy show --region eu`). The command token is
the first bare (non-flag, non-flag-value) token that names a subcommand at the
current depth. A flag value that happens to match a command name
(`--region show`) is consumed as the value, not treated as the command.

**Help.** Subcommand help (`deploy show --help`) must surface inherited global
args alongside the subcommand's own args, so required globals are visible from
any command's help.

### Requiring a command

By default a runnable that defines `commands` may still be invoked without
naming one — it runs at the parent level. Set `require-command = true` on the
runnable (or on any nested command that itself has commands) to make choosing a
command mandatory, matching `argparse`'s `add_subparsers(required=True)`, Click
groups, and `git`/`docker`/`kubectl`. Invoking that level with no command — or
with a bare token that is not one of its commands — errors and lists the
available commands; a mistyped token gets a `Did you mean` suggestion.

`require-command` is a **parent-level** flag: it attaches to the act of choosing
among the commands, not to any individual child. Put it on whichever level must
force a choice, and repeat it at each such level; omit it on a level that is
fine to run as-is. A leaf command (one with no `commands` of its own) never
takes the flag — there is nothing below to choose.

```toml
[db]
require-command = true

[db.commands.migrate]
[db.commands.seed]
```

```
db            # ✗  'db' requires a command. Available commands: migrate, seed
db migrate    # ok
```

Nested, required at both levels:

```toml
[cluster]
require-command = true

[cluster.commands.node]
require-command = true

[cluster.commands.node.commands.list]
[cluster.commands.node.commands.drain]
```

```
cluster              # ✗  'cluster' requires a command. Available commands: node
cluster node         # ✗  'cluster node' requires a command. Available commands: list, drain
cluster node drain   # ok
```

`require-command` is a static field, **not** an inferred one — it adds no row to
the inference table. It is enforced only when parsing real CLI arguments;
introspection via `load_spec()` (and therefore `emit`) is unaffected, and
emitted MCP/JSON-Schema tool sets already expose only fully-pathed leaf
commands. Each leaf tool's schema carries the **effective** argument set —
the inherited global args merged with the command's own (child overriding
parent of the same name) — matching what `parse()` accepts at runtime.

---

## Composing a Spec: `include` and `shared-args`

Two authoring features let a large `runspec.toml` be split into files and stop
repeating arg definitions. Both are resolved when the spec is **loaded**,
before inference, so every consumer (parsing, help, validation, emitted
schemas, `serve`, discovery) sees the same spec as if it had been written as
one file. A spec that uses neither loads exactly as before.

Resolution order: all `include`s first (over the whole tree), then all `use`s.
After resolution no `include`, `shared-args` or `use` key remains.

### `include`

A runnable table or a command table (at any depth) may contain

```toml
include = "relative/path.toml"
```

The named file is parsed as TOML and its top-level keys are merged into the
table. It holds the table's **body** (`description`, `autonomy`, `args`,
`groups`, `commands`, `shared-args`, `meta`, …), without the
`[<runnable>.commands.<c>]` prefix.

| Rule | Behaviour |
|---|---|
| Path base | Relative to the file that contains the `include` (`runspec.toml` or another included file) |
| Path form | `/`-separated, relative, ends in `.toml`. A backslash, an absolute path or a drive letter is an error |
| Boundary | After normalisation the path must stay inside the directory holding `runspec.toml`. Implementations reading the real filesystem also refuse a path that resolves (through a symlink) outside it |
| Nesting | An included file may itself contain `include` (at its top level, or on its own `commands`). Paths in it are relative to that file |
| Cycles | A file already on the chain from `runspec.toml` to the current table is an error naming the chain (`a.toml → b.toml → a.toml`) |
| Depth | At most 8 included files on one chain; deeper is an error |
| Conflicts | A key present both in the table and in the included file is an error. There is no override between files |
| Missing file | An error naming the file and the file that included it |

`include` is not allowed in `[config]` or inside `args`.

### `shared-args` and `use`

A `shared-args` table maps names to arg definitions, in any form an `args`
entry accepts (bare default or full table):

```toml
[config.shared-args]          # visible to every runnable in the file
proxy = {required = false, description = "HTTP proxy, host:port or 'direct'"}

[gitlab.shared-args]          # visible to gitlab and all its commands
project = {type = "str", required = false, env = "GITLAB_PROJECT"}

[gitlab.commands.create-issue.args]
project        = {use = "project", required = true}
source-project = {use = "project"}
```

An arg whose definition is a table containing `use = "<name>"` is replaced by
a copy of the named shared definition, then every other key in that table is
applied on top (local keys win). The arg keeps its own name.

Lookup: the `shared-args` of the table that holds the arg, then of each
parent up to the runnable, then `[config.shared-args]`; the nearest match
wins. Tables on sibling commands are not visible. Since includes are resolved
first, a `shared-args` table in a parent file is visible in included files.

Errors: an unknown name (implementations should suggest a close match), a
`use` value that is not a non-empty string, and a shared definition that
itself contains `use`. An unused shared definition is not an error.

### Tooling

- The JSON Schema for included files is published at
  `https://runspec.app/runspec-fragment.schema.json` (it references the main
  schema's runnable definition).
- Discovery that reads `runspec.toml` text without a filesystem (e.g. the
  console's remote scan) must also fetch the included files; implementations
  expose a text loader that takes a reader callback for this.

---

## Form Control Inference

When `ui` is not declared, implementations should infer the form control:

| Type / Property | Default control |
|---|---|
| `str` | Text input |
| `password` | Masked text input (characters hidden) |
| `int` or `float` | Number input |
| `path` | File/directory picker |
| `bool` or `flag` | Checkbox |
| `options` ≤ 4 items | Radio group |
| `options` > 4 items | Dropdown |
| `range` present | Slider |
| `multiple = true` | Multi-select |

An author may also declare `ui` explicitly to request a specific control. On a
`str` arg, `ui = "date"` / `"datetime"` / `"time"` / `"month"` requests a date /
date-time / time / month picker (the type stays `str`; the picked value is
written as an ISO-8601 string — `YYYY-MM-DD` for `date`, a full timestamp for
`datetime`, `HH:MM:SS` for `time`, `YYYY-MM` for `month`), and `ui = "color"`
requests a colour picker (value a `#rrggbb` hex string). On an `int`/`float` arg
with a `range`, `ui = "slider"` renders a slider (falling back to a number field
when no range is declared). On a `choice` arg, `ui = "multiselect"` renders a
multi-select — equivalently, a `choice` arg with `multiple = true` is a
multi-select by inference (the table above). Because a multi-select yields
several values, pair `ui = "multiselect"` with `multiple = true` so the arg is
genuinely multi-valued at parse time (each picked value is validated against
`options`); `multiple` alone is sufficient. These are all explicit opt-ins —
they are never inferred (apart from the `multiple = true → multi-select` row
above).

When `hint` is declared, implementations should surface it as the field's
placeholder text (an example or format hint shown in the empty control). It is
advisory only — it never changes parsing or validation.

---

## Emitted Schema Fields

When emitting to agent formats, implementations must include:

- `name` — runnable name
- `description` — runnable description
- `x-autonomy` — autonomy level
- `x-autonomy-reason` — reason if provided
- `x-output` — output type (`"text"` if not declared)
- `inputSchema` — JSON Schema object of all arguments

---

## Compliance

All implementations are tested against the fixtures in
`tests/integration/fixtures/`. A conforming implementation must produce
output that matches the canonical JSON in `tests/integration/compliance/`
for every fixture.

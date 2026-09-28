# `runspec exec` — run an installed runnable, recorded end-to-end

> **Status: v1 implemented** (`runspec` 0.62.0; `runspec-scheduler` 0.3.0;
> `runspec-console` 0.299.0). A core verb that runs one installed runnable as a
> child process and records the whole invocation — including the parts the
> runnable can't record about itself — in the per-venv runs index
> (`runspec_runs.db`) and audit logs. It's the wrapper the cron-backed shared
> scheduler ([cron-scheduler.md](cron-scheduler.md)) puts on its crontab lines, and
> the thing any other unattended caller (CI, the emitted Rundeck/Ansible jobs, a
> future systemd-timer backend) can use to get the same record.

## 1. Problem

A runnable records itself: `parse()` configures logging, and the logging hook
writes the `running` row at start and finalizes it at exit. That self-recording
has blind spots, all of which bite hardest when nobody is watching (cron):

1. **Runnables without `[config.logging]` record nothing.** The runs index follows
   the audit-log coverage, so there is no row and no log.
2. **Failures before logging starts vanish.** `configure_logging` runs after
   arguments are parsed and validated, so bad args, a `run_as` identity refusal,
   a denylist refusal, a missing binary or an unreadable `RUNSPEC_ENV_FILE`
   produce no record.
3. **The recorded exit code is inferred, not observed.** The atexit hook records
   `exit_code = 1 if exception else 0`, so `sys.exit(2)` is recorded as a success.
4. **Output is lost.** `print()` output and anything written straight to
   stdout/stderr outside the logger never reach the audit file; the cron backend
   sends both streams to `/dev/null`.
5. **Overlap skips are invisible.** The crontab line's `flock -n` guard exits 1
   silently when the previous fire is still running.

Only something *outside* the runnable's process can close these. The execution
logic for that already exists — `runspec serve`'s `tools/call` handler resolves
the binary and its `runspec.toml`, converts arguments, escalates `run_as`, and
captures output and exit code — but it's buried in an MCP JSON-RPC loop that cron
can't speak to.

## 2. Decision

- **A core verb, not a scheduler feature.** Every unattended caller has the same
  blind spots; putting the wrapper in `runspec-scheduler` would fix only schedules
  and duplicate the execution code a third time. Core owns the runs index, the log
  grammar and `become`, so the wrapper can write rows without reaching into
  another package's internals, and runspec-scheduler stays a *translator* (config
  → crontab lines), as its design says.
- **Name: `exec`, not `run`.** `run` was renamed to `jump` in the past and meant
  "run on a remote host"; reusing it would confuse old docs and muscle memory.
  (`cron-scheduler.md`'s example line said `runspec run backup`, which never
  existed — it now says `runspec exec`.)
- **Python-only for now,** like `rebrand`: it's an operational tool for Python
  venvs. It launches any installed runnable binary, so a Node runnable installed
  in the venv is recorded too. A Node port can follow if wanted.

## 3. Shape

```
runspec exec [--label L] [--trigger T] [--lock PATH] [--skip-if-running]
             [--capture-bytes N] [--timeout S] <runnable> [-- <args…>]
```

```
runspec exec (wrapper)                          runnable (child)
─────────────────────                           ─────────────────
mint run_id R
[--lock / --skip-if-running → busy?  → row R 'skipped', exit 0]
begin row R  (running, label, trigger, pid=wrapper)
spawn child  env RUNSPEC_RUN_ID=R ──────────▶   configure_logging adopts R,
tee + tail-capture stdout/stderr                  pops it from os.environ
                                                  begin row R (upsert: its pid, args, log file)
                                                  … work …
                                                  finish row R (inferred exit code)
child exits with rc ◀────────────────────────
finish row R  (status + exit_code from rc, wall duration)   ← wrapper wins
append exec_output + exec_result records to R's log file
exit rc
```

### 3.1 Resolution

The runnable is resolved exactly as `runspec serve` resolves it — installed
packages via `importlib.metadata`, the binary from the venv scripts dir, the
runnable's `runspec.toml` passed as `RUNSPEC_CONFIG`, and the declared `run_as`
resolved for this host and applied through `become`. Hidden (`discoverable =
false`) runnables are runnable (hidden ≠ disabled), and the `serve` context filter
doesn't apply. Everything after `--` is passed to the binary verbatim, so
subcommands work: `runspec exec cron -- add --schedule …`.

### 3.2 The shared executor

`serve`'s `tools/call` path is extracted into `runspec/execute.py`: `find_runnable`
(name → exec spec) and `build_command` (exec spec + argv + env → the final argv,
escalated when `run_as` is set). `serve` keeps its MCP-specific parts (argument
dict → flags, the secret env channel, the JSON-RPC envelope) and uses the shared
functions; `exec` uses the same ones. No behaviour change to `serve`.

### 3.3 One row per run — the run-id handoff

Two processes now want to write a row for the same run. The contract:

1. The wrapper mints the run id and writes the `running` row first (its own pid,
   `label`, `trigger`).
2. It passes the id to the child as **`RUNSPEC_RUN_ID`**. Core's
   `configure_logging` adopts it (when it looks like a run id) instead of minting
   a fresh one, and **removes it from `os.environ`** so a runnable that shells out
   to *other* runnables doesn't hand the same id to them.
3. The child's own start/finish writes land on the same row (upsert by `run_id`).
   The upsert **never clears `label`** (a column only the wrapper sets).
4. When the child exits, the wrapper finalizes the row with the **observed** exit
   code and wall-clock duration. It writes last, so its values win.

**Older child runspec (≤ 0.61).** It ignores `RUNSPEC_RUN_ID` and writes its own
row under a fresh id. The wrapper then *adopts* that row: after the child exits
it looks for a row on this host for the same runnable, started within the
wrapper's window, with no label, and not the wrapper's own. If exactly one
matches, it stamps the label and observed outcome onto that row and deletes its
own placeholder, so History still shows one run. If none or several match, it
keeps its own row (worst case: a duplicate, never a lost run).

### 3.4 Output capture

- Both streams are **tee'd**: passed through to the wrapper's own stdout/stderr
  (so an interactive or CI caller still sees them live) and tail-captured in
  memory — the last `--capture-bytes` bytes per stream (default 64 KiB; `0`
  disables capture).
- After the child exits, the wrapper appends up to three JSON records to the run's
  log file, in the normal audit-log shape (`ts`/`level`/`logger`/`message`/`extra`
  with `extra.run_id`):
  - `event = "exec_output"`, one per non-empty stream: `stream`, `text`,
    `total_bytes`, `truncated`;
  - `event = "exec_result"`: `exit_code`, `duration_ms`, `label`, `trigger`,
    `status`.
- **Which file:** the log file on the run's row when the child set one (so
  everything about a run is in one file), otherwise a new per-run file
  `{runnable}.{utc-ts}.{run_id}.log` in the run's log dir. Both follow the existing
  log grammar, so `runspec logs view --run`, `prune`, `compact`, the console
  History expand and `logs status` handle them with no changes. No new file type,
  so nothing escapes retention.
- The runs-index row stays slim — output is never stored in the DB.

### 3.5 Skips

- **`--lock PATH`** — an exclusive non-blocking lock (`fcntl.flock`) held for the
  child's lifetime. Race-free. If it's held, the run is **recorded as `skipped`**
  (a row with `status = 'skipped'`, `exit_code = 0`, zero duration, plus an
  `exec_result` record), a one-line note goes to stderr, and the wrapper exits 0.
  POSIX only; on Windows the flag is refused with a clear error.
- **`--skip-if-running`** — consults the runs index for a live `running` row with
  the same label (or the same runnable when no label is given). Best-effort: two
  wrappers starting in the same instant can both proceed. It needs no lock path,
  so it suits ad-hoc callers; the scheduler uses `--lock`.

`skipped` is a new `status` value. Readers that bucket by status treat it as
neither success nor failure (the console shows it as its own tag).

### 3.6 Everything else

- **Exit code:** the child's. `--timeout` kills the child and exits 124
  (recorded `failed`). A child killed by a signal exits `128 + signum`.
- **Signals:** SIGTERM/SIGINT/SIGHUP received by the wrapper are forwarded to the
  child; the wrapper still records the outcome after the child exits.
- **stdin** is inherited.
- **`[config.history] db = false`** in the runnable's `[config]` is honoured: no
  wrapper row. Output records are still written (the run's own audit).
- **Log dir:** the wrapper uses the same resolver as the logging hook
  (`{sys.prefix}/logs`, falling back to `~/logs`). With `run_as` escalation the
  child writes as another account, and if that account resolves a different dir
  the two halves split (wrapper row in one dir, child row in the other). The
  scheduler's crontab lines don't hit this: cron already runs them as the
  `run_as` account, so wrapper and child are the same user.

## 4. Schema (runs index v3)

Additive, like v1 → v2:

```sql
ALTER TABLE runs ADD COLUMN label TEXT;   -- set only by `runspec exec --label`
CREATE INDEX IF NOT EXISTS runs_label ON runs (label);
```

`status` gains the value `skipped`. `runspec history list` gains `--label`, and
`runsdb.list_runs(label=…)` the same filter. (The proposed incremental pull
schema in [history-incremental-pull.md](history-incremental-pull.md) moves to v4.)

## 5. The scheduler on top

A crontab line becomes:

```
# runspec:sched:nightly-backup
0 2 * * *  env RUNSPEC_ENV_FILE=… /opt/venvs/ops/bin/runspec exec --label sched:nightly-backup --trigger schedule --lock ~svc/.cache/runspec-scheduler/nightly-backup.lock backup -- --target prod >/dev/null 2>&1
```

The trailing redirect only silences cron's mail; the output is captured in the
run's log file. `runspec-scheduler status --json` joins three sources on the
schedule id: the config, the installed crontab lines, and the runs index rows
labelled `sched:<id>` (plus the last run's captured output). That's the read the
console's **Shared schedules** view makes over SSH.

## 6. Not in v1

- A Node port.
- Streaming output to the runs index or a live tail of a running `exec` (the
  output lands at exit; `runspec history running` shows it's in flight).
- Switching `emit --rundeck` / `--ansible` job templates to `runspec exec` —
  worth doing, but a separate change to generated artefacts.
- Console management of shared schedules (add/edit/run-now) — the Shared
  schedules view is read-only, per the cron-scheduler design.

## 7. Tests that must exist

- The executor extraction leaves `serve` behaviour identical (existing serve
  tests).
- Handoff: a child that adopts `RUNSPEC_RUN_ID` yields one row with the observed
  exit code (including a `sys.exit(2)` the child alone would record as 0), the
  label kept, and `RUNSPEC_RUN_ID` gone from the child's own environment.
- A child with no logging config still gets a row + output records.
- An older child (own run id) is adopted: one row, labelled.
- `--lock` busy → `skipped` row, exit 0; `--skip-if-running` against a live row.
- Capture: tail truncation at `--capture-bytes`, both streams, passthrough.
- `--timeout` → 124, `failed`.

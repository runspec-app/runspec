# Design: multi-writer audit logging (per-invocation files + `runspec logs`)

## Problem

runspec's file handler is always a *rotating* handler — `_make_file_handler`
(`logging_setup.py`) returns a `RotatingFileHandler` (size) or
`TimedRotatingFileHandler` (time), and the log path is fixed per runnable:
`{sys.prefix}/logs/{runnable}.log` (`_resolve_log_dir` → `~/logs` fallback).

That model is fine for a single user running a runnable locally. It breaks
when a runnable is run by **multiple users**, or by the **same user in
parallel**, against one shared deployment venv — which is exactly the target
deployment for `runspec-linux` (admin runnables installed into a shared venv,
run by a team).

Two distinct failures:

1. **File ownership.** `bin/greeter` always writes `logs/greeter.log`.
   Whoever runs it first creates that file, owned by them. With a default
   `umask 022` it's `rw-r--r--`, so the second user cannot write — even if the
   `logs/` directory itself is group-writable. (Directory write controls
   *creating* an entry; file write controls *appending* to an existing file.)

2. **Concurrent writes + rotation (the real problem).** Single small lines
   written `O_APPEND` are atomic under ~4 KB, so individual JSON records from
   different processes won't interleave mid-line. But **rotation is not
   safe**: `RotatingFileHandler.doRollover()` renames `greeter.log →
   greeter.log.1` and opens a fresh `greeter.log`. If process A rotates while
   process B holds an fd to the old inode, B keeps writing into the
   renamed/unlinked file — **silent log loss**. Two processes rotating near
   simultaneously clobber each other's backups. This is the well-known "Python
   logging is not multi-process safe" footgun, and runspec is *always* in a
   rotating mode, so config can't dodge it.

This document specifies a fix that keeps runspec's properties intact —
**zero runtime dependencies, file-based JSON audit trail, cross-platform** —
and turns the ops ergonomics into a first-class feature.

---

## Approaches considered (and why they were rejected)

| Approach | Multi-writer safe? | Shared trail? | Notes |
|---|---|---|---|
| **Per-user filename** `greeter.{user}.log` | ✗ | split | Collapses under a shared sudo/service account (one uid → one file) and under same-user parallel runs. |
| **Shared file + setgid + `umask 002`** | ✗ | ✅ | Fixes *ownership* only; rotation still loses logs. No permission trick fixes in-process rotation. |
| **Locked single file** (advisory `flock`/`msvcrt` + reopen-on-inode-change, à la `concurrent-log-handler`) | ✅ | ✅ | Genuinely works and stays zero-dep, but is the most code to own forever, locks per emit (serializes writers), and has NFS (`flock` reliability) + Windows (`os.rename` fails on a file held open) sharp edges. A **sentinel lockfile + PID liveness check is the trap version** — leaks on `kill -9`, racy under PID reuse; only kernel advisory locks are safe. |
| **syslog / journald sink** | ✅ | ✅ | OS serializes writers; but **journald requires systemd** (absent on Alpine/minimal containers/WSL1), reading the system journal needs `systemd-journal` group membership, and **Windows has no journald** (Event Log needs `pywin32` → violates the zero-dep rule). Fragments the audit model by OS. |
| **Per-invocation files + `runspec logs`** (chosen) | ✅ | ✅ on demand | Each invocation owns its own file → zero contention to manage. Ops ergonomics provided by a composable CLI stream instead of a shared file on disk. |

The per-invocation approach wins because it splits the problem along the seam
where each half is easy: the **write side becomes trivially safe** (no shared
file ⇒ no locks, no rotation race, identical behaviour on Windows), and the
**aggregation/retention** that per-invocation files would otherwise cost is
handled by a single cooperating CLI command rather than punted to the operator.

---

## Design

### Write side — per-invocation files (and *less* code)

Opt in via a new `[config.logging]` key (see "Open decisions" for the name; this
doc uses `store`):

```toml
[config.logging]
store = "per-run"     # default "single" = today's rotating file (unchanged)
```

When `store = "per-run"`, the file path becomes:

```
logs/{runnable}.{YYYYMMDDThhmmss}.{run_id}.log
```

- `run_id` is already minted per invocation as `uuid.uuid4()`
  (`logging_setup.py`), so collisions are impossible across parallel runs and
  shared service accounts alike.
- The timestamp prefix is purely so `ls`/glob sort naturally; **`mtime` is the
  source of truth** for age-based retention.
- Because each invocation owns its own file, **there is no rotation and no
  concurrency**. The handler drops to a plain `logging.FileHandler`. The
  `rotate`/`keep` knobs do not apply per file — that responsibility moves
  wholesale to `runspec logs` (below). Net: the write path gets *simpler*.

The JSON line format is unchanged — each record already carries timestamp,
level, `run_id`, and invoker (`_get_invoker()` captures the real human even
under `sudo`, which is what makes a shared service account still attributable).

### Ops side — `runspec logs`, a composable stdout stream

The only downsides of per-invocation files — no single tail-able stream, file
explosion, no rotation — each map to a verb. The guiding principle is
**"be a clean line stream on stdout that composes,"** not an app: ops live in
`grep`/`less`/`awk`/`sed`/`jq`, so the command must pipe cleanly.

```
runspec logs <runnable>                    # merged view: read all {runnable}.*.log,
                                           #   k-way merge by timestamp, one record per line
runspec logs <runnable> --follow           #   live tail across invocations (arrival-ordered)
runspec logs <runnable> --run <id> --since 1h --user alice   # filter on JSON fields
runspec logs <runnable> --json             # raw JSON lines (passthrough) for jq
runspec logs prune <runnable> --older-than 30d [--max-files N] [--max-total-size 1G]
runspec logs compact <runnable> --older-than 7d [--gzip]     # roll old runs into an archive, delete originals
```

Stream behaviour — the part that makes it feel like a real Unix tool:

- **Default output is a plain merged line stream on stdout.** Each invocation
  file is internally time-ordered, so a streaming k-way merge (heap keyed on
  each file's next-line timestamp) emits a globally sorted stream in bounded
  memory. This stream *is* "the one file."
- **TTY-aware.** Colour/pager only when `stdout.isatty()`; piped output is
  plain so `| grep`/`| awk` see clean columns.
- **SIGPIPE-clean.** When `| head` or `less` closes the pipe, exit silently —
  no `BrokenPipeError` traceback. (Restore `SIG_DFL` for `SIGPIPE` / catch
  `BrokenPipeError` at the top level.)
- **Text by default, `--json` for jq.** Default flattens each record to a
  grep/awk-friendly line, e.g.
  `2026-06-03T14:22:01 INFO run_id=… user=alice  message  {extra}`. Because
  invocations are interleaved, each line carries `run_id` + invoker inline so a
  grep'd line is self-identifying. `--json` passes raw JSON lines straight
  through.
- **`prune`/`compact` is one cooperating program**, so it can take a single
  advisory lock trivially — none of the N-arbitrary-writers hardness applies.
  `prune` replaces `keep`; `compact` replaces rotation.

Make many files look like one file *to tools that demand a path* (no extra code
— document the shell idiom):

```bash
awk '$2=="ERROR"' <(runspec logs greeter)
sed -n '100,200p'  <(runspec logs greeter --since 1h)
less               <(runspec logs greeter --follow)
```

`<(…)` process substitution backs a `/dev/fd/NN` path with the merged stream,
so any filename-expecting tool treats the invocation files as a single file. A
persistent FIFO/named-pipe export is possible later for long-lived pollers, but
process substitution covers the named cases without building anything.

---

## Deployment: read-only shared venv

The permission model the write side is designed for (and which
`_resolve_log_dir`'s write-probe + `~/logs` fallback already anticipates):

```
deployment-venv/            drwxr-xr-x  deployer staff   # r-x: traverse + import
  bin/                      drwxr-xr-x                    # x to run the runnables
  lib/python3.X/...         drwxr-xr-x                    # r to import installed code
  logs/                     drwxrwxr-x  deployer staff    # group-writable, setgid, NOT sticky
```

- `r-x` on the venv dirs is all other users need: `x` to traverse in and run
  `bin/runspec`, `r` to import packages. (Reaching a writable child only needs
  `x` on each parent — not `r` or `w`.)
- `logs/` = `2775` (setgid + group-writable, **not** sticky), shared group,
  `umask 002`. Each user's run creates its own per-invocation file
  (group-readable `664`), so **no file is ever shared ⇒ no ownership clash**.
- A deployer who is just a **group member** (no root) can run
  `runspec logs prune`/`compact` on a cron/systemd-timer, because deleting an
  entry in a group-writable non-sticky directory needs directory write, not
  file ownership.

### Pre-compile bytecode before locking down

A read-only `site-packages` means Python can't write `__pycache__/*.pyc`. It
degrades silently (imports still work) but recompiles in memory every run.
Pre-compile as the owner, before the chmod lockdown:

```bash
python -m compileall -f --invalidation-mode unchecked-hash deployment-venv/lib
```

`unchecked-hash` stamps the `.pyc` so Python trusts it without re-checking the
source on every import — combined with a read-only tree the cache is always
valid and Python never attempts a write. Belt-and-suspenders: set
`PYTHONDONTWRITEBYTECODE=1` in the runtime environment.

### Misbehaving packages (audit, don't pre-solve)

Most runtime writes do **not** land in the venv: `tempfile` → `/tmp`,
well-behaved caches → `~/.cache` (`$XDG_CACHE_HOME`), `pip` writes only at
install time (done by the owner). The risk is packages that write into their
own `site-packages` at runtime (downloaded data, compiled artifacts). Don't
prescribe a second venv shape up front — **document how to detect it**:

```bash
# run each runnable once as a non-owner user against the locked-down venv:
sudo -u tester deployment-venv/bin/<runnable> ...        # watch for PermissionError
# or trace writes outside /tmp, ~, and logs/:
strace -f -e trace=openat -P deployment-venv/lib <runnable> ... 2>&1 | grep O_WRONLY
```

Packages that fail this need a writable install and belong in a separately
built (per-user or writable) venv — left open until a real offender appears.

---

## Caveats (documented honestly)

- **Operational, not forensic, audit.** Non-sticky + group-writable `logs/`
  means any group member can delete invocation files (including their own). So
  this is an *operational* record (you trust the operators, you want a trail),
  **not** tamper-proof logging. Strong tamper-resistance fundamentally requires
  a sink the writer can't rewind (privileged syslog/journald collector), which
  we rejected for portability. For the runspec-linux "trusted sysadmins"
  model, operational-grade is the right fit.
- **NFS.** The per-invocation write side is NFS-safe (no shared file, no
  locks). Only `runspec logs compact/prune`'s single lock relies on
  `flock`; on NFS prefer running the timer from one host.
- **Windows.** Per-invocation files work for free (nothing shared ⇒ no
  `os.rename`-while-open or lock issues). The `runspec logs` command is pure
  file I/O and runs anywhere. Only the `chmod`/setgid deploy recipe is Unix —
  on Windows that's ACLs or per-user log directories.

---

## runspec-console impact (a hard dependency)

runspec-console reads the JSON log files directly for **two** features —
History and Analytics (`bridge.py`) — so the filename change is a breaking
change for it and must ship together.

How console consumes logs today:

- `get_history` / `_collect_host_records` glob `{runnable}.log` (or `*.log`)
  in `{venv}/logs` — locally via `Path.glob`, remotely by SSH-`cat`-ing the
  files with a `\x00RUNSPEC_LOG:<name>` frame per file.
- `_parse_log` derives the **runnable name from the filename**:
  `log_file.stem` locally, `basename "$f" .log` remotely. That name becomes the
  `runnable` field on every history/analytics record.
- `_parse_log_by_run_id` then groups records by `extra.run_id` — so console
  **already isolates invocations by run_id** (since runspec 0.18), and already
  reads the run-summary fields (`user`, `exit_code`, `duration_ms`, …) from
  `extra`.

What per-invocation filenames break:

1. **Globs.** `{runnable}.log` won't match `{runnable}.{ts}.{run_id}.log`.
   → change to `{runnable}.*.log` (and `*.log` for "all" still works).
2. **Runnable-name derivation.** `stem`/`basename .log` of
   `greeter.20260603T142201.<uuid>.log` yields
   `greeter.20260603T142201.<uuid>`, not `greeter` — every invocation would
   look like a *different* runnable, fragmenting both History and Analytics.

The clean fix — stop trusting the filename, read the record:

- The `run_summary` record already carries `extra.runnable` *and*
  `extra.run_id` (`logging_setup.py`). So console should derive the runnable
  name from the **record body**: group by `run_id`, take `runnable` from that
  group's summary entry. (Non-summary lines don't carry `runnable`, but every
  run_id has exactly one summary, so the group resolves it.)
- This makes console **agnostic to the filename scheme** — it works unchanged
  against both legacy `{runnable}.log` files and new per-invocation files, which
  keeps the migration forward- and backward-compatible. The glob is then only a
  *file-discovery* concern (`{runnable}.*.log` ∪ `{runnable}.log`).

Two interactions to respect:

- **`compact` output is now a console dependency.** Analytics
  (`_collect_host_records`) is *uncapped* — it aggregates every run_summary
  ever written. Per-invocation files multiply file count, so compaction is what
  keeps that tractable — but the compacted archive **must stay JSON-lines**
  (optionally gzipped) and console must learn to read `*.log.gz`. Compaction
  must not reformat or drop the records analytics reads — both the
  `run_summary` `extra` fields *and* the top-level `exc_structured` object
  (see below). Concatenate raw lines only.
- **Remote read cost.** SSH-`cat`-ing thousands of per-invocation files is
  slower than one file; `runspec logs compact/prune` on a timer bounds it, and
  console's History 200-cap already limits the hot path. Worth measuring before
  GA.

---

## Interaction with uncaught-exception records (#102)

`#102` (uniform uncaught-exception handling) added a second file-only logger,
`runspec.exception`, that writes one structured record on any uncaught
exception — a top-level `exc_structured` object (`type`, `message`, `module`,
full `frames`) plus the human `exc` traceback string. This **predates** the
per-invocation work and interacts with it, all favourably:

- **No structural conflict.** It flows through the same file handler, so it
  inherits `extra.run_id` (via `_RunIdFilter`) and lands in whatever file the
  invocation writes. In `store = "per-run"` mode that's the invocation's own
  file — so one file cleanly holds that run's lines + its `exc_structured`
  record + its `run_summary`. The crashing run still emits a `run_summary`
  (the excepthook sets `exit_code = 1` and the `atexit` summary still fires),
  so console's run_id grouping and runnable resolution are unaffected.
- **Console already tolerates it.** `_parse_log_by_run_id` groups by
  `extra.run_id` and ignores unknown records, so the exception record is
  grouped with its invocation today without breakage — it just isn't surfaced
  richly yet.
- **Opportunity (not required):** because `exc_structured.frames` is a clean
  table-ready stack, console's Analytics/History could render a real
  crash-frames view per failed run instead of only the summary's
  `exception.type/message`. Worth folding into the console step.
- **Compaction must preserve it** — `exc_structured` is a *top-level* field
  (not under `extra`), so the "concatenate raw lines, don't reformat" rule for
  `compact` covers it automatically. Flagged so a future structured rewrite
  doesn't silently drop it.

`#102` also added an "Uncaught exceptions" section to `spec/SPEC.md`; the
`store` key this design adds goes under the `[config.logging]` schema/section,
a separate area — no overlap.

---

## Scope / implementation surface

- `logging_setup.py` — `store` mode: per-invocation filename, swap rotating
  handler for plain `FileHandler` when enabled.
- `cli.py` — new `logs` entry in the `commands = {}` dict (`cmd_logs`) with
  `--follow/--since/--user/--run/--json` and `prune`/`compact` subverbs;
  TTY-aware + SIGPIPE-clean output.
- `spec/SPEC.md` + `schema/runspec.schema.json` + `docs/format.md` — add the
  `store` key under `[config.logging]` (schema is `additionalProperties:
  false`, so it must be added there first per the CLAUDE.md "change format →
  update SPEC first" rule).
- `CHANGELOG.md` + version bump.
- **Node parity** — `runspec-node` mirrors the logging + run-summary design, so
  the `store` mode and a `logs` command are required to keep parity (track as a
  follow-up node-* bump).
- **runspec-console** — must ship in lockstep: switch `bridge.py` history +
  analytics to derive the runnable from `extra.runnable` (record body) instead
  of the filename, widen globs to `{runnable}.*.log`, and read compacted
  `*.log.gz` archives. Without this, per-invocation logs fragment console's
  History and Analytics. Needs its own version bump + CHANGELOG entry.
  **Implementation spec: `console-bridge-per-invocation.md`.**
- Deploy recipe lands in a new `docs/` page (e.g. "Deploying a shared venv")
  wired into the mkdocs nav, once the mechanism is decided here. **Includes the
  copy-paste operator retention runbook** (cron + systemd timer for
  `runspec logs compact`/`prune`) — see `runspec-logs-command.md`.

## Rollout order

1. **Write side** — `store` mode + per-invocation filenames in
   `logging_setup.py`, plus schema/SPEC/format.md. Nothing consumes the new
   layout yet, so this is safe to land first behind the opt-in flag.
   **Implementation spec: `per-invocation-write-side.md`.**
2. **runspec-console** — make `bridge.py` filename-agnostic (read
   `extra.runnable`). Because this also works against today's `{runnable}.log`
   files, it can land *before or with* step 1 with no regression.
3. **`runspec logs`** — view/prune/compact, plus console reading `*.log.gz`.
   **Implementation spec: `runspec-logs-command.md`** (includes the operator
   cron/systemd retention runbook).
4. **Node parity** + the deploy docs page.

---

## Open decisions

1. **`store` mode is opt-in (recommended), not default.** Today's single
   rotating file stays the default — it's correct for local/single-user, and
   flipping it would relocate everyone's logs. Shared-venv deployments set
   `store = "per-run"`.
2. **Output is text by default, `--json` opt-in (recommended)** — ops use
   line/column tools (`grep`/`awk`/`sed`/`less`); `jq` users opt into raw JSON.
3. **Key/value naming** — `store = "per-run"` vs `"single"` is a placeholder;
   confirm the key name (`store`? `mode`?) and value spelling before touching
   the schema.

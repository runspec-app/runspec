# Design: `runspec logs` — viewing + operator-run retention

Companion to `multi-writer-audit-logging.md` (step 3). Specifies the command
that gives per-invocation logs their single-file ergonomics **and** the
retention tooling that replaces in-process rotation. Written operator-first:
in `store = "per-run"` mode, rotation is *your* scheduled job, and this is the
tool + the runbook for it.

> **Why this exists.** `per-run` mode deliberately has **no in-process
> rotation** — that's what makes it safe for multiple users / parallel runs (no
> file is ever shared, nothing renames a file out from under another writer).
> The cost moves to ops: per-invocation files accumulate, so you schedule
> cleanup. runspec ships the commands; you wire them to cron/systemd.
> (The default `store = "single"` mode still auto-rotates in-process — this
> burden only applies once you opt into `per-run`.)

## Command surface

```
runspec logs <runnable> [filters]          # VIEW: merged stream (default verb)
runspec logs prune   [runnable] [policy]   # RETENTION: delete old per-run files
runspec logs compact [runnable] [policy]   # ROTATION: roll old files into an archive
```

First token after `logs` selects the verb; if it isn't `prune`/`compact` it's
treated as a runnable name for the view. All verbs default to **all runnables**
in the venv's `logs/` when no runnable is given — so one scheduled command
covers the whole environment.

---

## VIEW — `runspec logs <runnable>`

The "make N files look like one file" stream (full rationale in the parent
doc). Recap of the contract:

```
runspec logs greeter                       # merged, timestamp-sorted, one record/line
runspec logs greeter --follow              # live tail across invocations
runspec logs greeter --since 1h --user alice --run <id>
runspec logs greeter --json                # raw JSON lines for jq (passthrough)
```

- Plain line stream on **stdout**, streaming k-way merge by timestamp (bounded
  memory; each per-run file is already time-ordered internally).
- **TTY-aware** (colour/pager only when `isatty()`), **SIGPIPE-clean**
  (`| head`/`less` closing the pipe exits silently).
- Text default carries `run_id` + `user` inline (records are interleaved, so
  each line must self-identify); `--json` passes raw lines through.
- Reads both live `{runnable}.{ts}.{run_id}.log` files **and** compacted
  `{runnable}.archive.*.log.gz` archives, so history survives compaction.
- Composes with everything via process substitution:
  `awk '$2=="ERROR"' <(runspec logs greeter)`.

---

## ROTATION / RETENTION — the operator runbook

### `prune` — delete old per-invocation files

```
runspec logs prune [runnable] \
    [--older-than 30d] [--max-files N] [--max-total-size 5G] \
    [--dry-run]
```

- Policies combine: a file is removed if it violates **any** given policy
  (older than the age, beyond the newest-N, or over the size budget — oldest
  first). At least one policy is required (no policy → no-op + error, so a
  misfired cron line can't wipe everything).
- **`--dry-run` prints exactly what would be deleted and removes nothing** —
  always the first thing an operator runs.
- **Safety:** only ever matches the per-run patterns
  (`{runnable}.{ts}.{run_id}.log` and `{runnable}.archive.*.log.gz`). It will
  **never** touch a `single`-mode `{runnable}.log` active file, even if both
  modes' files share a directory.

### `compact` — roll old files into an archive (the "rotation")

```
runspec logs compact [runnable] [--older-than 7d] [--gzip] [--dry-run]
```

- Concatenates per-run files older than `--older-than` (raw JSON lines, in
  timestamp order — **no reformatting**, so every field including the #102
  `exc_structured` survives) into
  `{runnable}.archive.{YYYYMMDD}.log` (or `.log.gz` with `--gzip`), then deletes
  the originals. The `{runnable}.` prefix keeps it matchable by console's
  `{runnable}.*` discovery glob.
- Append-merges into the day's existing archive if it already exists
  (idempotent across a day's runs).

### Concurrency, permissions, exit codes

- **One cooperating program**, so `prune`/`compact` take a single advisory
  `flock` on `logs/.runspec-logs.lock` — two timer fires (or a manual run during
  a scheduled one) serialize cleanly. None of the N-writers hardness applies.
- **No root needed.** With the deploy recipe's setgid + group-writable,
  non-sticky `logs/`, any operator in the shared group can prune/compact every
  user's files (directory write governs deletion, not file ownership).
- **Exit non-zero on failure** so cron mail / `systemctl status` surfaces
  problems; `--dry-run` always exits 0.

### Schedule it — cron

```cron
# deploy user's crontab — nightly 02:30: archive >7d-old runs, drop >90d-old archives
30 2 * * *  /opt/runspec-venv/bin/runspec logs compact --older-than 7d --gzip && \
            /opt/runspec-venv/bin/runspec logs prune   --older-than 90d
```

### Schedule it — systemd timer

```ini
# /etc/systemd/system/runspec-logs-gc.service
[Unit]
Description=runspec per-invocation log compaction + retention

[Service]
Type=oneshot
User=deploy
ExecStart=/opt/runspec-venv/bin/runspec logs compact --older-than 7d --gzip
ExecStart=/opt/runspec-venv/bin/runspec logs prune   --older-than 90d
```
```ini
# /etc/systemd/system/runspec-logs-gc.timer
[Unit]
Description=Nightly runspec log GC
[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true        # catch up if the box was off at 02:30
[Install]
WantedBy=timers.target
```
```
systemctl enable --now runspec-logs-gc.timer
runspec logs prune --older-than 90d --dry-run     # preview before trusting the timer
```

These exact snippets go in the **"Deploying a shared venv"** docs page (step 4)
and the `docs/logging.md` per-run subsection, so operators get copy-paste, not
prose.

### Future: register with the native runspec scheduler (no hand-rolling)

The documented runspec scheduler (`console-plan.md` §10, "Scheduling model —
DECIDED") is the long-term answer to "operators shouldn't hand-roll cron." Two
reasons it fits this job exactly:

- The scheduler's own design **already lists "log rotation cleanup"** as a
  canonical *after-hours admin cleanup* job (the second of its two scheduling
  flavors — "must fire whether the console UI is open or not").
- Its mechanism is "register a task that calls `runspec <…>` directly" (Windows
  Task Scheduler in v1, in the `\runspec-console\` task folder, user scope).
  `runspec logs compact`/`prune` is just another `runspec` invocation, so it
  registers through the **same** path as any scheduled runnable — via the
  console schedule editor (a typed form) instead of editing crontabs by hand.

So the end state is: an operator adds a "nightly log GC" schedule in the console
UI, and it runs `runspec logs compact --older-than 7d --gzip` + `prune` on the
target host — no cron/systemd by hand.

Two integration notes for whoever builds that:

1. **Platform timing.** The scheduler's v1 is **Windows-only**; Linux/macOS is
   explicitly deferred (the doc notes `cron`/`launchd` can later swap in behind
   the same interface). Since the per-run deployment target is Linux, the
   **cron/systemd runbook above is the path today**, and the same
   `runspec logs` command slots into the native scheduler once its Linux backend
   lands — no command change, just where the schedule is defined.
2. **Maintenance jobs vs runnables.** The scheduler/History model assumes a
   *runnable* + its `{runnable}.log`. `runspec logs` is a maintenance subcommand,
   not a runnable — so the schedule editor needs to represent it, and ideally
   `runspec logs compact`/`prune` should emit their **own run_summary** to the
   audit trail (they already run inside runspec's logging) so a GC run shows up
   in console History like any other job. That closes the loop: rotation becomes
   observable in the same console that schedules it.

---

## Optional: config-driven defaults

To keep the cron line short and policy in one place, `prune` may read defaults
from `[config.logging]` when a flag is omitted:

```toml
[config.logging]
store         = "per-run"
retain        = "90d"     # default for `prune --older-than`
compact-after = "7d"      # default for `compact --older-than`
```

CLI flags always win. This is additive and can land after the core verbs;
flagged as a follow-up, not required for v1. (Adds two schema keys → same
lockstep as `store`.)

## Implementation surface

- `packages/python/runspec/runspec/cli.py` — add `"logs": cmd_logs` to the
  `commands` dict (`cli.py:54`); `cmd_logs(args)` dispatches view/prune/compact
  and parses its own flags (matching the existing per-command convention).
- A small `runspec/logs.py` module for the merge/prune/compact logic (keeps
  `cli.py` thin; unit-testable without the CLI).
- **Node parity** — same `logs` command in `packages/node/src/cli.ts`; the
  merge/prune/compact logic mirrors Python. Tracked as a node-* bump.
- Reuses `_resolve_log_dir`'s directory logic to find `logs/` (Python:
  `{sys.prefix}/logs`; Node: `{project_root}/logs`).

## Tests

- **View:** k-way merge ordering across 3 per-run files; `--json` passthrough;
  SIGPIPE (`| head`) exits 0 with no traceback; reads a `.log.gz` archive.
- **prune:** `--older-than`/`--max-files`/`--max-total-size` each delete the
  right set; **`--dry-run` deletes nothing**; **never deletes `{runnable}.log`**
  (single-mode safety); no-policy is a no-op error.
- **compact:** archives the right files, preserves raw lines incl.
  `exc_structured`, deletes originals, append-merges same-day; `--gzip`
  round-trips; archive matches console's discovery glob.
- **lock:** two concurrent compacts serialize (no double-archive / lost lines).

## Open questions

1. **`gc` convenience verb?** A single `runspec logs gc` = compact-then-prune
   per the config defaults, so the cron line is one token. Nice-to-have; the
   two-command `&&` works today. Recommend deferring until config defaults land.
2. **Archive granularity** — daily (`archive.{YYYYMMDD}`) vs monthly. Daily is
   simpler and bounds per-archive size; revisit if file counts stay high.

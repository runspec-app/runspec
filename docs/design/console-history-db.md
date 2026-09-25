# Run-index DB — a shared per-venv SQLite runs table (`runspec_runs.db`)

> **Status: in progress.** The agreed design for a per-venv SQLite index of
> runnable runs, written by core `runspec`, read by the runspec-console History /
> Logs tabs and (later) the standalone scheduler. **Phase 1 (core write path)** is
> implemented in the PR that carries this revision, after the §12 validation; the
> remaining phases — including the §8.1 log-prune provenance added in this
> revision — are the reviewable spec for the follow-up work.

## 1. Problem

The console's History and Logs tabs reconstruct their view by **streaming and
parsing the JSON-lines audit logs** over SSH — one read per host, and every row's
status/duration comes from a log file's summary line, so a row costs a file read.
The merged 0.283/0.284 work made that read cheap-ish (bounded newest-N, one shell
inventory per host, per-host incremental render), but it's still *parse files
every time*, the date window can't cheaply limit a remote fetch, and there is **no
notion of what is running right now** — a log summary only exists once a run has
*finished*.

Three things want a better substrate:

1. **History / Logs** want an *indexed* read: `SELECT … WHERE ts >= cutoff ORDER BY
   ts DESC LIMIT N` instead of streaming + parsing, so the window is a real
   `WHERE` and a row never costs a file read.
2. A **live "what's running"** view — impossible from end-of-run summaries alone.
3. The **shared scheduler** (`docs/design/venv-scheduler.md`) needs shared,
   authoritative in-flight state to answer "is this already running?" (avoid
   double-runs) and "what's running" (a fleet view) without its own bookkeeping.

`sqlite3` is in the Python standard library, so this costs core **no new
dependency** — it stays stdlib-only.

## 2. Shape: a runs table with a lifecycle

A single SQLite DB per venv holds one `runs` table. Core writes a row at the
**start** of every runnable invocation (`status = running`) and **finalizes** it
at the end (`completed` / `failed`, with exit code + duration). This gives:

- **live view** — `SELECT … WHERE status = 'running'`;
- **history** — `SELECT … WHERE status != 'running' ORDER BY start_ts DESC`;
- the scheduler reads the same rows for overlap-detection.

Writing at start is what enables the live view and the scheduler benefit, and it
is why orphan handling ([§6](#6-orphans-nothing-reads-as-running-forever)) is a
first-class concern.

## 3. The DB is a derived index; the logs stay authoritative

The JSON-lines audit log remains the **source of truth** and the durable audit
trail. The DB is a fast **index** over it, with one addition (live state):

- **Finished-run rows** are fully reconstructable from the log summaries —
  `runspec history reindex` rebuilds the DB from the logs. A corrupt / missing /
  perms-blocked DB never loses history: the console falls back to the log-parse
  read that exists today.
- **`running` rows** are transient live state that only the DB holds (the log's
  summary is written at the end). Losing the DB loses "what's running now"
  transiently — it self-heals as runs finish (their summaries land in the logs)
  and as orphans are reaped. History is never lost.

So the invariant is: **logs = durable audit trail; DB = fast index + transient
live layer.** This is what makes it safe to add a second store to core.

## 4. Location & permissions — ride the existing `2775 logs/` setup

The DB lives **in the run's log directory**, resolved exactly as a run's log file
is: `{sys.prefix}/logs/runspec_runs.db`, falling back to `~/logs/runspec_runs.db`
when the venv's own `logs/` isn't writable (the same `log_dirs()` order the audit
log uses). **Not** at the venv root.

Rationale — once multi-writer is a hard requirement, `logs/` is the correct home:

- **`create-venv --group` already makes `logs/` `2775`** (setgid + group-writable
  + group-owned). A shared DB and its SQLite `-wal` / `-shm` sidecars are created
  *in that setgid dir*, so they inherit the group and several users can write —
  the whole ballgame for multi-writer SQLite, with zero new provisioning for
  venvs made the normal way.
- **The venv root stays non-group-writable**, which we want: it holds `bin/` and
  `site-packages`; making it group-writable would let anyone in the group replace
  a binary/package (privilege escalation). Only `logs/` is shared, by design.
- **`runspec logs prune` / `compact` never touch it** — they match `*.log` /
  `*.log.gz` by the log-name grammar; a `.db` is not matched.
- **The console already reads both `{venv}/logs` and `~/logs`**, so reading a DB
  in each dir and unioning the result is symmetric with today's file read.

### 4.1 Who establishes writability — a layered split

Writability is two separate concerns; only one is core's to do.

| Concern | Owner | How |
|---|---|---|
| The **dir** is setgid + group-owned + group-writable (privileged, one-time) | `runspec-linux` + console | `create-venv --group` (exists); a **repair path** for legacy/hand-made venvs — the `runspec-linux` **`share-venv-logs`** runnable (shipped in 0.14.0) sets `logs/` `2775` + optional chgrp as owner/root, to be surfaced from the console as a one-click **"Make logs shared"** action over SSH-as-owner (reusing the `sudo -n -u <owner>` escalation the denylist writes use — phase 3) |
| Each **writer** creates its files group-writable (per-write, unprivileged) | core `runspec` | on first create, `chmod 0o664` the DB **before** entering WAL (so SQLite makes the `-wal`/`-shm` sidecars group-writable to match — see §12); also heal an owned, non-group-writable DB on open; create `logs/` if absent. (`umask` alone can't do this — it only *clears* bits, never adds group-write.) |

Core deliberately does **not** try to `chmod`/`chgrp` the dir itself: it usually
runs as a non-owner with no right to, and a silent failure there is worse than not
trying. Instead it **degrades gracefully** — if the shared dir isn't writable for
this user the best-effort write no-ops (their runs fall back to `~/logs`'s DB, or
simply aren't indexed; History still falls back to log parse) and it surfaces a
one-line hint ("`{venv}/logs` isn't shared-writable — run `create-venv --group` or
the console's *Make logs shared*"), never a traceback.

Consequence: the scheduler's overlap check is **most reliable on a properly-shared
venv** (all writers in the one venv DB). On an unshared venv, writes scatter to
per-user `~/logs` DBs and the check sees only its own — the nudge toward running
the fix. The console's fleet view still unions every DB it reads, so *visibility*
stays complete either way.

## 5. Schema

One `runs` table, deliberately **slim** — never log lines or tracebacks (those
stay in the log file, fetched per-row on expand), so rows are a few hundred bytes
and 100k runs is single-digit MB.

```sql
CREATE TABLE IF NOT EXISTS runs (
  run_id       TEXT PRIMARY KEY,
  runnable     TEXT NOT NULL,
  command      TEXT,             -- subcommand path, space-joined; NULL if none
  status       TEXT NOT NULL,    -- running | completed | failed | crashed
  exit_code    INTEGER,          -- NULL while running
  start_ts     TEXT NOT NULL,    -- ISO-8601 UTC
  end_ts       TEXT,             -- NULL while running
  duration_ms  INTEGER,          -- NULL while running
  operator     TEXT,             -- OS user that launched it
  run_as       TEXT,             -- escalated identity, if any
  initiated_by TEXT,             -- user | llm
  trigger      TEXT,             -- manual | schedule | mcp | trigger | ...
  runbook      TEXT,
  pid          INTEGER,
  host         TEXT,
  proc_start   TEXT,             -- liveness token vs PID reuse (see §6)
  log_file     TEXT,             -- basename, for per-log fetch on expand
  args         TEXT              -- small JSON; secrets already excluded upstream
);
CREATE INDEX IF NOT EXISTS runs_start_ts ON runs (start_ts);
CREATE INDEX IF NOT EXISTS runs_status   ON runs (status);
CREATE INDEX IF NOT EXISTS runs_runnable ON runs (runnable);
```

Columns map 1:1 to today's `run_summary` fields, so `reindex` is a direct
translation. Schema evolves **additively** (`ALTER TABLE ADD COLUMN` guarded by a
`schema_version` set via `PRAGMA user_version`); no destructive migrations — the
§8.1 log-prune-provenance columns are the first such addition (a v1 → v2 delta).

## 6. Orphans — nothing reads as "running forever"

A `running` row whose process dies without finalizing must never look alive
forever. Two layers:

1. **Finalize on the way out** — a top-level `try/finally` plus `atexit` plus
   `SIGTERM` / `SIGINT` handlers close the row to `completed` / `failed` even when
   the run raises or is asked to quit. Covers exceptions and graceful/silent
   exits (the common cases).
2. **Liveness-checked reaping** for the hard residue (`SIGKILL`, segfault, power
   loss) that *can't* finalize. A `running` row carries `pid` + `host` +
   `proc_start` (a per-process start token — e.g. `/proc/<pid>` start time or the
   boot id — so PID reuse can't produce a false "alive"). A row is only ever
   *reported* running if that process is actually alive; otherwise it is reaped to
   `status = crashed`. Liveness is checked **where the process lives**:
   - the next run in the venv sweeps stale rows at start (bounded, idempotent);
   - the console / scheduler reap-on-read;
   - the console's live-view read script checks `/proc/<pid>` (or `kill -0`) on the
     host in the *same* round-trip, so even an un-reaped orphan never *displays* as
     running;
   - a `max_run_hours` age ceiling ([§10](#10-config)) is the final backstop.

A crashed run is recorded as `crashed` — a useful signal — not silently lost.

## 7. Concurrency & the write path

- **WAL mode** + a short `busy_timeout` (a few seconds). Multiple writer
  processes are fine under WAL.
- Writes are **best-effort and non-fatal**: a busy / locked / unwritable DB never
  fails or delays the run (its real work is already done). `sqlite3.OperationalError`
  and friends are swallowed and logged, not raised into the run.
- **Two writes per run** (start + finalize). Start is a single indexed `INSERT`;
  finalize is an **UPSERT** keyed on `run_id` (`INSERT … ON CONFLICT(run_id) DO
  UPDATE`), so finalize is correct whether or not the start row landed (e.g. a very
  short run whose async start write lost the race just gets one finished row).
- The write may run on a **background thread** so it never sits on the run's
  critical path; ordering is safe because finalize is an UPSERT on the same key.

## 8. Retention & pruning — the DB stays small by construction

Independent of log pruning (rows outlive logs on purpose), three mechanisms stack:

1. **Slim rows, no blobs** ([§5](#5-schema)) — the dominant factor.
2. **Self-bounding on write (default, no cron):** each write opportunistically
   trims to a retention policy — `DELETE FROM runs WHERE start_ts < cutoff` and/or
   keep-newest-N — so the table behaves like a ring buffer and can't grow unbounded
   even if no maintenance command is ever run. Bounded indexed delete, best-effort
   like the write.
3. **Space actually reclaimed:** the DB is created with `PRAGMA auto_vacuum =
   INCREMENTAL`, so deletes free pages back to the OS incrementally — no blocking
   full `VACUUM` on a shared, actively-written file.

**DB retention is its own policy, separate from log pruning.** The model:

> the DB is a **bounded, fast index of recent history**; the log files are the
> **deeper archive**, pruned on their own `runspec logs prune` schedule;
> `runspec history reindex` bridges them.

So you can keep, say, 90 days / 50k rows in the DB while logs prune at 7 days —
expanding an older row then says **when and by whom** the log was pruned (§8.1),
not a bare "file not found". Explicit control via
`runspec history prune --older-than … / --max-rows …` (and a Logs-tab button) sits
on top of the automatic ring. Reaped `crashed` rows are ordinary finished rows and
age out the same way.

### 8.1 Prune provenance — why the log is missing

Because a DB row **outlives its log file on purpose** (rows are the bounded index;
logs prune on their own schedule), "row present, log file gone" is a *normal,
expected* state — not a fault. Today the console would try to fetch the log lines
on expand and get a bare "file not found"; instead the row should say *when* the
log was pruned and *by whom*, turning a blank gap into "log pruned by `alice` on
2026-09-20 — record retained".

**Schema delta (additive, v1 → v2).** Two nullable columns describe the *log
file's* availability — never the run outcome, which `status` still owns:

```sql
ALTER TABLE runs ADD COLUMN log_pruned_at TEXT;   -- ISO-8601 UTC; NULL = log present
ALTER TABLE runs ADD COLUMN log_pruned_by TEXT;   -- OS user that ran the prune
```

`ALTER TABLE ADD COLUMN` is metadata-only (no table rewrite); existing rows read
`NULL` and older readers ignore the new columns, so it is a clean forward/backward-
compatible bump of `PRAGMA user_version` (1 → 2), applied on open when a column is
absent (a `PRAGMA table_info(runs)` guard). Because the DB is a derived index, a
botched migration is never data loss — worst case it is rebuilt via `reindex`.

**Where it's stamped — core, at the one place a log dies.** `runspec logs prune`
deletes the file (`logs.py::prune` → `f.unlink(...)`). Right after the unlink, a
**best-effort** update stamps any row in that dir's DB that points at the file:

```sql
UPDATE runs SET log_pruned_at = ?, log_pruned_by = ? WHERE log_file = ?;  -- basename match
```

- Living in **core** means both a direct-CLI `runspec logs prune` and the
  console's shell-out prune (`runspec logs prune --scope venv`, 0.56.0) stamp for
  free — no console-side logic.
- `log_pruned_by` is the **OS user running the prune** (`getpass.getuser()`),
  meaningful on the shared `2775` dir where different service accounts prune.
- An archive/rotated file backing several `run_id`s stamps **all** rows whose
  `log_file` matches its basename.
- It is **best-effort and non-fatal**, like every other DB write here (§7): a
  locked/unwritable DB never blocks the prune, and the stamp runs *after* the
  unlink so a DB hiccup can never strand the delete.

**`compact` moves, it doesn't delete.** `logs.py::compact` folds per-run files into
a dated archive, so the content still exists under a new name. That path
**repoints** the affected rows' `log_file` to the archive (so per-log fetch keeps
working) rather than marking them pruned; only real deletion sets `log_pruned_at`.

**`reindex` must not clobber the stamp.** `reindex` UPSERTs from surviving
`run_summary` records; a pruned log has no record to rebuild from, so `log_pruned_*`
are **prune-path-owned** and excluded from the reindex UPSERT column set (otherwise
a row it touches would have its stamp nulled).

**Visibility needs DB retention ≥ log retention.** The note only shows while the
row outlives its log file — i.e. the DB ring (`retention_days` / `max_rows`, §10)
must be at least as generous as the `runspec logs prune` age. If the row is itself
ring-pruned there is simply no row (fine — the note is a bonus for that window).
`runspec history list --json` surfaces the two columns so the console read (§14)
renders them with no extra query.

## 9. Read path — no cold-start

- **Local host:** the console/scheduler query the DB in-process.
- **Remote host (console):** a tiny **read-only** query via `{venv}/bin/python -c
  "<query>"` — interpreter + stdlib `sqlite3` only, **no `runspec` import**, so
  none of the package-import cold-start the merged work removed. One round-trip per
  log-dir DB (`{venv}/logs` + `~/logs`), the same shape as the `find` inventory.
  For the live view the same script resolves `running`-row liveness on the host.
- This drops straight onto the console's existing per-host incremental render +
  timing strip: each host's DB query renders as it returns.

`runspec history …` CLI verbs exist for humans/scripts and for the privileged
maintenance ops, but the console's hot read uses the direct `python -c` path.

## 10. Config

A small new `[config.history]` section in the console/runspec config (stdlib-only,
core stays zero-dep), all defaulting on/generous:

```ini
[config.history]
db = true              # write the runs DB (master gate)
retention_days = 90    # ring-buffer age bound (0 = no age bound)
max_rows = 50000       # ring-buffer row cap (0 = no row cap)
max_run_hours = 24     # orphan age ceiling for the reaper (0 = never age-reap)
```

Resolution follows the existing `[config.logging]` precedence. `db = false` makes
core skip the DB entirely (History falls back to the log-parse read).

## 11. CLI surface

| verb | autonomy | what |
|---|---|---|
| `runspec history list --json [--since --status --runnable --limit]` | read | the query the console read mirrors |
| `runspec history running` | read | the live in-flight rows (liveness-filtered) |
| `runspec history prune [--older-than --max-rows]` | confirm | explicit prune on top of the ring |
| `runspec history reindex` | confirm | rebuild the DB from the log summaries |
| `runspec history reap` | confirm | force an orphan sweep |

## 12. Validation — done (informs §4.1 / §9)

A throwaway multi-process harness on a `2775` setgid dir settled the two parts
with teeth **before** core started writing the DB. Results:

- **Reader open mode → `mode=ro`.** A separate-process `file:{db}?mode=ro` reader
  *does* see rows another process committed to the WAL but hasn't checkpointed
  (so the console's `{venv}/bin/python -c` read uses `mode=ro` — read-only, no
  lock contention, no accidental writes).
- **Group-writability → `chmod 0o664` *before* WAL, not `umask`.** SQLite creates
  the DB `0644` and `umask` only *clears* bits, so it never adds group-write. But
  `chmod 0o664` the main DB **before** `PRAGMA journal_mode=WAL` makes SQLite
  create the `-wal`/`-shm` sidecars group-writable to match (verified: all three
  `g+w`); doing it *after* the sidecars exist leaves them `0644`. So core chmods
  the DB before WAL on create (one chmod, no per-sidecar work) and group
  *ownership* comes from the setgid dir.
- **Concurrent writers under `busy_timeout` → clean** (0 errors, all rows land),
  confirming the best-effort multi-writer model.

(The sandbox is single-user, so the perms *mechanics* — mode bits + setgid
ownership propagation — were validated rather than two literal OS users; the
chmod-before-WAL rule is OS-level and user-independent.)

## 13. Scheduler reconciliation

`runspec-scheduler` is a per-venv daemon whose config already lives at
`{sys.prefix}/runspec_scheduler.toml` (the same venv-root convention), it is
runnable-only, and it shells out to the runnable — so:

- its fires are **auto-recorded** (the runnable runs through core → a row with
  `trigger = schedule`), no scheduler-side bookkeeping;
- before firing it can check the runs table for an active `running` row of that
  runnable to **avoid overlap / double-runs**;
- the future standalone/cross-host scheduler gets its **fleet view** by reading
  each venv's DB (per-venv is exactly its unit), matching the doc's direction.

The schema in [§5](#5-schema) already carries what it needs (`runnable`, `status`,
`start_ts`, `host`, `trigger`). Any scheduler-specific column is added additively.

## 14. Console integration

- **History** swaps `get_history_slim` / the log-parse read for a per-host DB
  query (log-parse stays as the fallback when `db = false` or the DB is
  absent/corrupt); the per-host incremental render + timing strip are unchanged.
- **Logs tab** keeps its `find` disk-usage scan (bytes / gzip / archives are
  filesystem facts the DB doesn't track); the DB can augment the per-runnable rows
  with run-count / last-run. A **live "running"** indicator (from the `running`
  rows) is a natural addition to History and/or the sidebar.
- A **"Make logs shared"** action (Logs tab / Settings) runs the `runspec-linux`
  fix-perms repair over SSH-as-owner for venvs not created with `--group`.
- Per-log fetch is unchanged in spirit — a row's `run_id` + `log_file` fetch the
  one file on expand (today's `get_run_detail` / `logs_view`). When the file is
  gone and `log_pruned_at` is set (§8.1), the expand panel shows "log pruned by
  `<user>` on `<date>` — record retained" in place of a fetch error, so a
  deliberately-pruned log reads as expected rather than broken.

## 15. Rollout / phasing

1. **Core write + schema + lifecycle + orphans + reindex + prune**, behind
   `[config.history] db` (default on), after the §12 validation. Ships in a
   `runspec` minor; no format change (no `spec/SPEC.md` touch), so this is
   core-internal. **(implemented — `runspec` 0.57.0.)**
2. **`runspec-linux` fix-perms** repair path — the **`share-venv-logs`** runnable
   (`runspec-linux` 0.14.0 / `runspec-linux-core` 0.7.0): sets an existing venv's
   `logs/` to `2775` + setgid (+ optional chgrp) so the DB and logs are
   group-writable, without recreating the venv. (`create-venv --group` already
   covers the DB for a fresh venv, via the setgid dir.) **(implemented — the
   runnable; the console "Make logs shared" one-click surfacing is phase 3.)**
3. **Console read swap** — History/Logs read the DB with log-parse fallback; the
   live "running" view; the "Make logs shared" action. **(implemented, in slices:
   the "Make logs shared" one-click action — `runspec-console` 0.285.0; the
   History read swap with per-venv log-parse fallback — 0.286.0; the live
   "Running now" strip — 0.287.0, via `get_running_runs`, reading `running` rows
   with pid-liveness resolved on the owning host per §9.)**
4. **Log-prune provenance** (§8.1) — core stamps `log_pruned_at` / `log_pruned_by`
   on `runspec logs prune` (a v1 → v2 additive column bump), and the console shows
   "log pruned by X on Y" when an expanded row's log is gone. Core stamping is
   independent (it can ship in any `runspec` minor); the display rides phase 3's
   read swap. **(core implemented — `runspec` 0.58.0: schema v2 + stamp-on-prune +
   repoint-on-compact + reindex-safe; the console display is the next slice.)**
5. **Scheduler** reads the table for overlap-detection (when that work is picked
   up).

Each phase is independently shippable: core writing the DB harms nothing on its
own (best-effort, gated), and the console read is purely additive over the
existing fallback.

## 16. Non-goals

- **Not** a replacement for the audit logs — they stay the source of truth
  ([§3](#3-the-db-is-a-derived-index-the-logs-stay-authoritative)).
- **Not** a cross-host database — one DB per venv; the fleet view is the reader
  unioning per-venv DBs.
- **Not** a general query/reporting engine — the table is the History/Logs/
  scheduler index, nothing more.
- Core does **not** self-provision privileged directory permissions
  ([§4.1](#41-who-establishes-writability--a-layered-split)).

# Incremental history pull — a cursor-synced local cache of the runs index

> **Status: implemented** — core `runspec` 0.63.0 (schema v4, §3–§4) and
> `runspec-console` 0.300.0 (the cache, §5). Decisions taken on the open
> questions are recorded in §10.
>
> A design for making the console's History tab read from a
> **local cache** that is kept current by pulling only what changed in each
> remote venv's runs index (`runspec_runs.db`) since the last pull. It builds on
> [console-history-db.md](console-history-db.md) (the per-venv index itself) and
> is the "sync engine lite" follow-up discussed alongside the Logs-tab
> **Runs index…** maintenance work (`runspec` 0.62.0 / `runspec-console`
> 0.299.0).

## 1. Problem

The History tab now reads the per-venv runs index instead of parsing log files
(console 0.286.0), but every read is still a **full windowed query over SSH**:
`_slim_history_db_remote` runs one `{venv}/bin/python -c` snippet per host that
returns the newest `_HISTORY_CAP` finished rows in the date window, every time.
The 15 s `_REMOTE_HISTORY_TTL` cache softens the 25 s poll, but:

1. **Opening History waits on the fleet.** Nothing is shown for a host until its
   SSH round-trip returns, even though almost all of those rows were already on
   screen a minute ago.
2. **Every poll re-ships the same rows.** A quiet host returns the identical 200
   rows on every refresh; the only thing that changed is usually nothing.
3. **The window caps what you can see.** Because each read re-fetches, the read is
   bounded (200 rows/host) to keep it cheap — so a busy host's older runs fall off
   History even though the index holds 90 days of them.
4. **Nothing survives a restart.** A fresh console starts from zero for every host.

The runs index is an unusually easy thing to sync: each row has a unique
`run_id`, exactly **one writer** (the process that ran it, or the host's own
reaper), and becomes **immutable** once finished apart from a handful of
maintenance stamps. There is no multi-writer conflict to resolve — the host is
always the authority. That makes a cursor-based pull the right tool; CRDTs or a
sync framework would be solving a problem we don't have.

## 2. Shape

```
remote venv (authority)                          console (cache)
┌──────────────────────────────┐                 ┌─────────────────────────────┐
│ runspec_runs.db               │   pull:        │ history_cache.db            │
│  runs(…, seq)                 │  "since seq N" │  runs(host, dir, run_id, …) │
│  runs_tombstones(run_id, seq) │ ─────────────▶ │  cursors(host, dir,         │
│  sync_meta(db_id, seq, floor) │  ◀── deltas ── │          db_id, seq)        │
└──────────────────────────────┘                 └─────────────────────────────┘
                                                   ▲ History reads here, instantly
```

- Every write to a remote `runs` row stamps it with a **per-DB monotonically
  increasing `seq`**. Deletes leave a **tombstone** with its own `seq`.
- The console keeps, per `(host, logs dir)`, a **cursor** `(db_id, seq)` and asks
  "everything with `seq > N`". It applies the deltas to a local SQLite cache.
- **History reads the cache first** — instant, even offline — and a background
  pull brings each host up to date, emitting an update when anything changed.

The console never writes back. Multiple consoles each keep their own cache; the
host stays the single source of truth, so there is nothing to reconcile.

## 3. Remote side (core `runspec`) — schema v4

### 3.1 The change sequence

A single counter lives in a new `sync_meta` key/value table, and **SQLite
triggers** keep it and the rows in step:

```sql
ALTER TABLE runs ADD COLUMN seq INTEGER;
CREATE INDEX IF NOT EXISTS runs_seq ON runs (seq);

CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value);
-- db_id   : random UUID minted when the DB (or its sync schema) is created
-- seq     : last assigned change sequence
-- floor   : lowest seq a cursor may still resume from (tombstone retention)

CREATE TRIGGER IF NOT EXISTS runs_seq_ins AFTER INSERT ON runs BEGIN
  UPDATE sync_meta SET value = value + 1 WHERE key = 'seq';
  UPDATE runs SET seq = (SELECT value FROM sync_meta WHERE key = 'seq')
   WHERE rowid = NEW.rowid;
  DELETE FROM runs_tombstones WHERE run_id = NEW.run_id;  -- a restored row
END;
CREATE TRIGGER IF NOT EXISTS runs_seq_upd AFTER UPDATE ON runs
  WHEN NEW.seq IS OLD.seq BEGIN      -- skip the trigger's own seq write
  UPDATE sync_meta SET value = value + 1 WHERE key = 'seq';
  UPDATE runs SET seq = (SELECT value FROM sync_meta WHERE key = 'seq')
   WHERE rowid = NEW.rowid;
END;
```

**Why triggers, not application code.** Every writer path — `begin_run`,
`finish_run`, the reaper, ring-prune, `mark_log_pruned`, `repoint_logs`,
`reindex` — would otherwise need to remember to bump `seq`, and one miss makes a
change invisible to every console forever. More importantly, triggers live **in
the database file**, so an **older runspec (0.57–0.62) writing a v4 DB still
bumps `seq`** — its `INSERT`/`UPDATE` statements name explicit columns and never
touch `seq`, and the trigger fills it in. A mixed-version shared venv (the
service account on one runspec, an operator on another) stays correct.

**Why the sequence is safe to use as a cursor.** SQLite has a single writer per
database: write transactions serialize, and `seq` is assigned inside them. A
reader therefore can never observe `seq = N + 1` committed while `N` is still in
flight — the classic hole in "`WHERE id > cursor`" pulls against Postgres
sequences doesn't exist here. (`mode=ro` readers see committed WAL frames,
validated in [console-history-db.md §12](console-history-db.md).)

### 3.2 Deletes → tombstones

A cursor pull can't see a row that is gone. Prune (ring-prune on finalize, and
`runspec history prune`) deletes rows, so a delete trigger records it:

```sql
CREATE TABLE IF NOT EXISTS runs_tombstones (run_id TEXT PRIMARY KEY, seq INTEGER NOT NULL,
                                           deleted_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS runs_seq_del AFTER DELETE ON runs BEGIN
  UPDATE sync_meta SET value = value + 1 WHERE key = 'seq';
  INSERT OR REPLACE INTO runs_tombstones (run_id, seq, deleted_at)
    VALUES (OLD.run_id, (SELECT value FROM sync_meta WHERE key = 'seq'),
            strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
END;
```

**A re-inserted row clears its own tombstone** (the insert trigger above) — e.g.
`reindex` restoring a row that ring-prune deleted. Otherwise one pull could carry
both "deleted" and the row, and the result would depend on the order the reader
applied them. With the clear, a run_id is never in both lists of a pull.

Tombstones are bounded the same way the ring is: `_maintain` drops tombstones
older than 30 days (`_TOMBSTONE_DAYS`, matching the console's cache window — a
console offline for up to a month resumes with a delta) or beyond 50,000
(`_TOMBSTONE_CAP`), and raises `sync_meta.floor` to the highest seq it dropped. A cursor **below the floor** has missed deletes it can no
longer learn about, so the pull answers `reset` (§4.3) instead of a delta.

Most ring-prune deletes are of rows the console has already aged out of its own
cache (§5.3), so tombstones mainly matter for an explicit `prune --max-rows` that
cuts into recent history.

### 3.3 Identity: `db_id`

`reindex`, a deleted-and-recreated DB, or restoring a backup all restart `seq`.
The random `db_id` in `sync_meta` changes whenever the sync schema is created
from scratch, and a cursor carries it: **a different `db_id` means "your cursor
is meaningless — resync"**. `reindex` into an existing DB keeps its `db_id` (the
upserts flow through the triggers like any other write, so an incremental pull
picks them up).

### 3.4 Migration (v3 → v4)

On a read-write open, `_migrate` adds the column, tables and triggers (all
`IF NOT EXISTS`), mints `db_id`, and **backfills** `seq` for existing rows in
`rowid` order in one statement — a one-time `UPDATE` over at most `max_rows`
(50k) rows, cheap on the first write after upgrade. Like v1 → v2 this is
additive: older readers ignore the new column and tables.

**Tested behaviours worth knowing** (SQLite 3.45): an `ON CONFLICT DO UPDATE`
upsert fires the update trigger; a zero-row `UPDATE` and an `INSERT OR IGNORE`
conflict do not; enabling `recursive_triggers` doesn't double-count (the `WHEN`
guard). A *no-op* upsert (identical values) does bump `seq` — so `reindex`
re-sends every row it touches once. That's deliberate: `reindex` is rare, and the
alternative (a `WHEN` comparing every column) would silently break the day a
column is added.

The migration runs in one `BEGIN IMMEDIATE` transaction (two writers migrating at
once can't hand out the same seq twice), and it creates the triggers **after**
the backfill. `finish_run` decides "did my UPDATE match a start row?" from the
statement's own `rowcount` — not `con.total_changes`, which counts the migration's
writes on the same connection.

**Don't trust `user_version`.** An older runspec opening the DB runs
`PRAGMA user_version=<its own version>` unconditionally, so a v4 DB can read back as an older version after an
old writer touched it. Readers detect v4 by **structure** (`sync_meta` exists and
`runs` has `seq`) — which is also what the stdlib snippet (§4) checks.

## 4. The pull

### 4.1 Wire shape

The existing remote-read pattern — one `{venv}/bin/python -c <snippet> <json>`
per host, stdlib `sqlite3` only, `mode=ro`, no `runspec` import — gains a sync
snippet. Request:

```json
{"dirs": {"/opt/venvs/ops/logs": {"db_id": "7f3…", "seq": 18422},
          "~/logs":              {"db_id": null,   "seq": 0}},
 "limit": 2000, "window_start": "2026-07-01T00:00:00+00:00"}
```

Response, per dir:

```json
{"/opt/venvs/ops/logs": {"mode": "delta", "db_id": "7f3…", "seq": 18431,
                         "rows": [ … ], "deleted": ["r_91…"], "more": false}}
```

| `mode` | meaning | console action |
|---|---|---|
| `delta` | cursor valid; `rows` are those with `seq > cursor` (finished and running), `deleted` the tombstones past it | upsert rows, delete tombstoned ids, advance cursor |
| `reset` | `db_id` changed, cursor below `floor` or above the DB's `seq`, or first sync | drop this dir's cached rows; `rows` is a windowed snapshot (`start_ts >= window_start`); cursor = the snapshot's `seq` |
| `legacy` | DB has no `seq` (a runspec without the pull has never written it) | today's windowed full read; no cursor stored |
| `none` | no DB | log-parse this dir exactly as today |

`window_start` filters deltas too: a reader that keeps only a window never needs
changes to rows older than it, and the cursor still advances past them (when a
page isn't capped, the cursor is the DB's current `seq`, not the last row's).

A capped `reset` continues as deltas from the last delivered row's `seq`: every
in-window row with a lower `seq` was delivered, and anything written since has a
higher one.

`more: true` means the delta was capped at `limit`; the console applies it,
advances the cursor to the last row's `seq`, and pulls again. Deltas are applied
in `seq` order, so a capped pull always leaves the cache consistent up to its
cursor.

The snapshot for `reset` is read in **one read transaction** together with the
current `sync_meta.seq`, so a write that lands during the snapshot is either in
the snapshot or has a `seq` greater than the stored cursor — never lost.

### 4.2 Running rows

Deltas include `running` rows (a start is a change), so the cache also knows
what *was* running. The **Running now** strip still asks the host directly
(`_running_runs_remote`), because liveness is a pid check that only the owning
host can make; the cached running rows are a hint, never shown as live without
that check.

### 4.3 When a pull runs

- On **History open**: render the cache immediately, then pull each
  `history_targets()` host concurrently (the existing per-host fan-out).
- On the **History poll** (25 s) while the tab is visible — a quiet host's pull
  returns an empty delta, a few hundred bytes.
- **After a run completes** where `_invalidate_remote_history` fires today, pull
  that host instead of invalidating.
- After a **Runs index… → Reindex/Prune** (console 0.299.0): pull that host; a
  reindex that recreated the DB comes back as `reset`.

Pulls ride the pooled SSH connection the refresh cycle already holds, so a pull
of a quiet host is one small exec on a warm transport.

## 5. Console side

**Remote hosts only.** The local host already reads its own indexes in-process
(`mode=ro`, instant), so it bypasses the cache; the cache exists to take SSH off
the History tab's critical path.

### 5.1 Cache store

`{app_dir}/history_cache.db` (`config.history_cache_path()`), **machine-local and
never synced** (like `conversations/` and the marketplace cache).
`history_cache.HistoryCache`:

```sql
CREATE TABLE recs (host TEXT, dir TEXT, key TEXT, ts TEXT, rec TEXT,  -- rec = HistoryRecord JSON
                   PRIMARY KEY (host, dir, key));
CREATE INDEX recs_host_ts ON recs (host, ts);
CREATE TABLE cursors (host TEXT, dir TEXT, db_id TEXT, seq INTEGER, PRIMARY KEY (host, dir));
CREATE TABLE hosts (host TEXT PRIMARY KEY, synced_at TEXT, error TEXT);
```

It stores **mapped records** (the `HistoryRecord` the tab renders, via
`db_row_to_record` at apply time), not raw rows. That lets one table hold both
kinds of dir a cached host can have:

- a **cursor dir** (`db_id` set) — a v4 index, kept current by deltas;
- a **snapshot dir** (`db_id` NULL) — a pre-v4 index read directly, or a dir with
  no index (log-parsed, stored under a pseudo-dir since the log stream doesn't say
  which dir a file came from) — replaced wholesale when refreshed.

Only finished runs are stored; a delta row that is `running` is dropped (the
Running-now strip asks the host live, §4.2). A transient `OperationalError`
(locked, disk full) is swallowed; a corrupt file (`DatabaseError`) is deleted and
rebuilt.

### 5.2 Reads and pulls

`get_history_slim(host, since_days)` for a remote host with a 7d/30d window:

- **first time** — pull in line (so the first view is complete), then read the
  cache; if the host has no v4 index, remember that for 10 minutes and read
  directly;
- **afterwards** — read the cache at once (window + a 1,000-row cap in SQL) and
  kick a background pull (one at a time per host, skipped if one finished in the
  last 5 s).

A pull is one `db_sync_cmd` round-trip per page (`_PULL_PAGE` 2,000 rows, up to
10 pages; a longer first reset continues on the next pull). Snapshot dirs cost a
full read, so they are refreshed at most every **2 minutes** — or at once when a
run finished on the host or after Runs index maintenance, which call
`_kick_history_pull(host, force=True)` through `_invalidate_remote_history`. A pull
that changed anything emits `runspec:history_updated {host}`, and the History tab
refetches that host alone.

### 5.3 Retention

The cache keeps a **30-day** window (the History tab defaults to 7 days and offers
7 / 30 / 90 / All): the pull passes `window_start` (so neither snapshots nor deltas
carry older rows) and trims older records on every pull. The 7d and 30d views read
the cache; **90d and All read the host directly**, exactly as before the cache.

### 5.4 Fallbacks — nothing gets worse

- A host with **no** v4 index, the local host, the 90d/All views and
  `[history] cache = false` take the pre-cache read path unchanged.
- A corrupt cache is deleted and rebuilt (it is derived data).
- A pull failure leaves the cache as-is and records the error;
  `history_sync_status()` reports `{cached, synced_at, error}` per host and the
  host's chip in the per-host strip turns amber: **"as of 3m ago"**.
- Hosts removed from `runspec_hosts.toml` (or renamed) are dropped from the cache
  when the tab next lists its targets; a venv removed from a host entry is dropped
  on that host's next pull.
- The pull snippet is stdlib-only (like the existing read snippets), so it runs
  under any venv's python whatever runspec it has. It mirrors core's
  `runsdb.changes`, and `tests/test_history_cache.py` runs both against the same
  indexes to keep them in step.

## 6. Scheduler reuse

`runspec-scheduler` reads its own venv's index in-process, so it doesn't need the
pull at all. The pull *does* become the substrate for the planned **standalone
shared scheduler's fleet view** (reading many venvs' indexes) — the same
cursor/delta contract works for any reader, not just the console. Exposing it as
a core verb (`runspec history since --cursor <db_id>:<seq> --json`) is cheap once
the snippet exists, and gives non-Python readers (Node pack, scripts) the same
contract.

## 7. What this deliberately isn't

- **Not bidirectional.** The console never writes remote rows; maintenance goes
  through `runspec history …` verbs as intents the host executes.
- **Not a CRDT or framework.** Single writer per row + host authority means
  last-write-wins by `seq` is exact, not an approximation.
- **Not cross-console sharing.** Consoles don't see each other's caches; they
  converge because they read the same authority.
- **Not a log-line cache.** Row-expand still fetches `logLines` on demand from the
  log file, as today.

## 8. Rollout

1. **Core `runspec` (next minor after 0.62)** — schema v4: `seq`, `sync_meta`, tombstones,
   triggers, backfill migration, tombstone retention in `_maintain`, structural v4
   detection. Pure addition: every existing read keeps working.
2. **Console** — `history_cache.db` + the sync snippet + cursor store; History
   reads the cache with pull-behind; `history_updated` event; "as of" freshness
   label. Gated by `[history] cache = true` (default on) with `false` restoring
   today's read path exactly.
3. **Optional** — `runspec history since …` verb (§6); raise the per-host cap.

## 9. Tests that must exist

- Trigger correctness: every writer path (`begin_run`, `finish_run`, `_reap`,
  `_prune`, `mark_log_pruned`, `repoint_logs`, `reindex`) advances `seq`; a delete
  leaves a tombstone.
- **Old-writer compatibility:** a DB migrated to v4, then written by the 0.60
  write path (simulated by explicit-column SQL + `PRAGMA user_version=2`), still
  gets `seq` on every row and is still detected as v4.
- Snapshot-then-delta: a write committed between snapshot and first delta is
  delivered exactly once.
- `reset` on `db_id` change and on cursor < `floor`; `more` pagination converges.
- Console: cache hit renders without SSH; legacy/none dirs bypass the cache;
  retention trims; a corrupt cache file is rebuilt.

## 10. Decisions (were open questions)

- **Cache window: 30 days.** Covers the 7d default and the 30d view from the
  cache; 90d / All read directly. Caching the full 90-day / 50k-row retention
  would make a busy venv's first `reset` a multi-MB pull for views rarely opened.
- **Tombstone retention: 30 days** (not 14), matching the cache window.
  Tombstones are a run_id + seq + timestamp each, so a month costs little, and a
  laptop console offline over a holiday resumes with a delta.
- **The local host bypasses the cache** (§5).
- **The Logs tab's per-runnable run counts** stay on their current path — out of
  scope here.

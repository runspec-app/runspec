# Incremental history pull — a cursor-synced local cache of the runs index

> **Status: proposed.** A design for making the console's History tab read from a
> **local cache** that is kept current by pulling only what changed in each
> remote venv's runs index (`runspec_runs.db`) since the last pull. It builds on
> [console-history-db.md](console-history-db.md) (the per-venv index itself) and
> is the "sync engine lite" follow-up discussed alongside the Logs-tab
> **Runs index…** maintenance work (`runspec` 0.62.0 / `runspec-console`
> 0.299.0). Nothing here is implemented yet.

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
CREATE TABLE IF NOT EXISTS runs_tombstones (run_id TEXT PRIMARY KEY, seq INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS runs_seq_del AFTER DELETE ON runs BEGIN
  UPDATE sync_meta SET value = value + 1 WHERE key = 'seq';
  INSERT OR REPLACE INTO runs_tombstones (run_id, seq)
    VALUES (OLD.run_id, (SELECT value FROM sync_meta WHERE key = 'seq'));
END;
```

Tombstones are bounded the same way the ring is: `_maintain` drops tombstones
older than `tombstone_days` (default 14) and raises `sync_meta.floor` to the
highest seq it dropped. A cursor **below the floor** has missed deletes it can no
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
| `reset` | `db_id` changed, cursor below `floor`, or first sync | drop this dir's cached rows; `rows` is a windowed snapshot (`start_ts >= window_start`); cursor = the snapshot's `seq` |
| `legacy` | DB has no `seq` (a runspec without the pull has never written it) | today's windowed full read; no cursor stored |
| `none` | no DB | log-parse this dir exactly as today |

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

### 5.1 Cache store

`{app_dir}/history_cache.db`, **machine-local and never synced** (like
`conversations/` and the marketplace cache). Two tables:

```sql
CREATE TABLE runs (
  host TEXT, dir TEXT, run_id TEXT,
  -- the remote runs columns, verbatim (db_row_to_record maps them as today)
  runnable TEXT, command TEXT, status TEXT, exit_code INTEGER, start_ts TEXT,
  end_ts TEXT, duration_ms INTEGER, user TEXT, user_target TEXT, run_as TEXT,
  agent INTEGER, trigger TEXT, skill TEXT, autonomy TEXT, pid INTEGER,
  remote_host TEXT, log_file TEXT, args TEXT, log_pruned_at TEXT, log_pruned_by TEXT,
  seq INTEGER,
  PRIMARY KEY (host, dir, run_id)
);
CREATE INDEX runs_host_start ON runs (host, start_ts);
CREATE TABLE cursors (host TEXT, dir TEXT, db_id TEXT, seq INTEGER, synced_at TEXT,
                      PRIMARY KEY (host, dir));
```

Rows keep the remote column names, so `utils_mod.db_row_to_record` — the one
tested place that maps a DB row to a `HistoryRecord` — is reused unchanged.

### 5.2 Reads

`get_history_slim(host)` becomes: read the cache for that host (window + cap
applied in SQL), return it, and schedule a pull. When the pull applies changes it
emits `runspec:history_updated {host}` and the History tab refetches that host
from the cache — the same per-host incremental render the tab already does,
just sourced locally.

With the cache local, **the 200-row per-host cap can rise** (to e.g. 2 000):
it now bounds a local SQL query, not an SSH transfer.

### 5.3 Retention

The cache keeps the History window it serves (default 30 days, following
`_HISTORY_DEFAULT_DAYS`/the widest window the UI offers) and drops older rows on
each pull. That also absorbs remote ring-prune deletes of old rows without
needing their tombstones.

### 5.4 Fallbacks — nothing gets worse

- `mode = legacy` / `none` dirs are read exactly as today (windowed DB read /
  log-parse) and **not cached** — so correctness never depends on the new path.
- A corrupt or unwritable cache is deleted and rebuilt (it is derived data).
- A pull failure leaves the cache as-is and marks the host's last-synced time in
  the History tab ("as of 3 min ago"), instead of showing nothing.
- Hosts removed from `runspec_hosts.toml` (or renamed) have their cache rows
  dropped on the next pull cycle.

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

## 10. Open questions

- **Cache window vs. the UI's "all time" option** — cache the full remote
  retention (90 days / 50k rows per venv) instead? It's local disk only, but the
  first `reset` of a busy venv would then be a multi-MB pull. Proposal: cache the
  default window; "all time" falls back to a direct windowed read.
- **Tombstone retention default (14 days)** — long enough that a laptop console
  offline over a holiday resumes with a delta rather than a reset?
- **Should the Logs tab's per-runnable run counts come from the cache** too, once
  it exists? Probably, but out of scope here.

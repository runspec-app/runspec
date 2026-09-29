"""Per-venv SQLite runs index (``runspec_runs.db``).

A **derived index** over the JSON-lines audit logs: core writes a ``running`` row
at the start of every invocation and finalizes it (``completed``/``failed``) at
exit, so the console History/Logs tabs (and a scheduler) get an indexed read and a
live "what's running" view without parsing log files. The logs stay the source of
truth — :func:`reindex` rebuilds the DB from them, and every read here has a
log-parse fallback in the console.

Design: ``docs/design/console-history-db.md``. Key properties enforced here:

* **Best-effort, never fatal.** Every public entry point swallows all errors — a
  busy, locked, or unwritable DB must never fail or delay a run (its real work is
  already done). Telemetry, not correctness.
* **Shared multi-writer.** The DB lives in the run's log dir (``{sys.prefix}/logs``
  or ``~/logs`` — the same resolver the log file uses). On first create the file
  is ``chmod 0o664`` **before** WAL is entered, so SQLite makes the ``-wal``/
  ``-shm`` sidecars group-writable to match; the ``2775`` setgid ``logs/`` dir
  (``create-venv --group``) supplies group ownership. WAL + ``busy_timeout`` let
  several runnable processes write at once (validated: docs §12).
* **Bounded.** Slim rows; a ring-buffer prune (age + row cap) on the finalize path
  keeps the file small; ``PRAGMA auto_vacuum=INCREMENTAL`` reclaims space.
* **Orphan-safe.** A row stuck ``running`` (SIGKILL/crash — no ``atexit``) is reaped
  to ``crashed`` by pid-liveness (``proc_start``-guarded, same host) or a max-age
  ceiling; readers liveness-filter too, so nothing ever *reads* as running forever.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DB_NAME = "runspec_runs.db"
# v2 (§8.1): + log_pruned_at / log_pruned_by. Additive, forward/backward-
# compatible — ALTER TABLE ADD COLUMN is metadata-only, existing rows read NULL,
# older readers ignore the new columns.
# v3 (docs/design/runspec-exec.md): + label — set only by `runspec exec --label`,
# never cleared by a later upsert (the child's own writes don't know it). Also
# introduces the `skipped` status value (an exec skip — lock busy / already running).
# v4 (docs/design/history-incremental-pull.md): + seq / sync_meta / runs_tombstones
# and the triggers that maintain them — a per-DB change sequence so a reader (the
# console's history cache) can pull only what changed since its cursor. Detected
# by *structure* (``_has_sync``), never by user_version: an older runspec writing
# the DB resets user_version to its own number, but the triggers (which live in
# the DB file) keep stamping ``seq`` on its writes regardless.
_SCHEMA_VERSION = 6
# v5: + events (JSON {level: count} — the run's log-record counts by level) and
# exception (JSON {type, message} — the uncaught exception, no traceback), taken
# from the run_summary at finalize. They let a reader (the console's Analytics)
# aggregate from the index instead of parsing log files. Written with COALESCE so
# a later finalize that doesn't know them (`runspec exec` finalizing its child's
# row) never clears them.
# v6 (docs/design/venv-logs-inbox.md): + os_user (the kernel-attested account —
# the writer's effective uid for a direct row, the inbox file's owner for a swept
# one), source ("direct" / "inbox") and exec_uid (the `runspec exec` wrapper's
# uid, for binding a wrapper and its child across the inbox). Kept on conflict
# like label, so a writer that doesn't know them never clears them.
_EXCEPTION_MESSAGE_MAX = 500
_BUSY_TIMEOUT_MS = 2000

_COLUMNS = (
    "run_id",
    "runnable",
    "command",
    "status",
    "exit_code",
    "start_ts",
    "end_ts",
    "duration_ms",
    "user",
    "user_target",
    "run_as",
    "agent",
    "trigger",
    "skill",
    "autonomy",
    "pid",
    "host",
    "proc_start",
    "log_file",
    "args",
)

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
  run_id       TEXT PRIMARY KEY,
  runnable     TEXT NOT NULL,
  command      TEXT,
  status       TEXT NOT NULL,
  exit_code    INTEGER,
  start_ts     TEXT NOT NULL,
  end_ts       TEXT,
  duration_ms  INTEGER,
  user         TEXT,
  user_target  TEXT,
  run_as       TEXT,
  agent        INTEGER,
  trigger      TEXT,
  skill        TEXT,
  autonomy     TEXT,
  pid          INTEGER,
  host         TEXT,
  proc_start   TEXT,
  log_file     TEXT,
  args         TEXT,
  -- §8.1 log-prune provenance: the log FILE's availability (never the run
  -- outcome — that stays `status`). NULL log_pruned_at = the log file is present.
  log_pruned_at TEXT,
  log_pruned_by TEXT,
  -- v3: the `runspec exec --label` tag (e.g. "sched:<id>"); NULL for direct runs.
  label         TEXT,
  -- v5: from the run_summary — log-record counts by level, and the uncaught
  -- exception's type + message. JSON text; NULL when unknown.
  events        TEXT,
  exception     TEXT,
  -- v6: attribution — who actually wrote the row (kernel-attested), whether it
  -- came straight from a run or was swept from the inbox, and the exec wrapper.
  os_user       TEXT,
  source        TEXT,
  exec_uid      INTEGER
);
CREATE INDEX IF NOT EXISTS runs_start_ts ON runs (start_ts);
CREATE INDEX IF NOT EXISTS runs_status   ON runs (status);
CREATE INDEX IF NOT EXISTS runs_runnable ON runs (runnable);
"""


def db_path(log_dir: Path) -> Path:
    """The runs DB beside the run's logs (``{log_dir}/runspec_runs.db``)."""
    return Path(log_dir) / DB_NAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── process-liveness token (orphan reaping) ─────────────────────────────────────


def proc_start_token() -> str:
    """A stable per-process token that survives PID reuse — the Linux
    ``/proc/<pid>/stat`` start-time (field 22, clock ticks since boot). ``""``
    where ``/proc`` is unavailable (the reaper then falls back to bare pid
    existence + the age ceiling)."""
    return _read_proc_start(os.getpid())


def _read_proc_start(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
            data = fh.read()
        # Field 22 is starttime; fields after comm (which may contain spaces and
        # is parenthesised) are split on the text following the last ')'.
        tail = data[data.rfind(")") + 1 :].split()
        return tail[19] if len(tail) > 19 else ""
    except (OSError, IndexError, ValueError):
        return ""


def _pid_alive(pid: int | None, proc_start: str | None) -> bool:
    """Is the process that wrote a ``running`` row still alive *on this host*?

    ``/proc``-based when we have a start token (guards PID reuse); else a
    non-destructive existence probe. Only meaningful for rows whose ``host`` is
    this host — the caller filters on that."""
    if not pid:
        return False
    if proc_start:
        token = _read_proc_start(int(pid))
        if token:
            return token == proc_start
        # No token: the process is gone — or /proc hides it (`hidepid=2` hides
        # other users' processes entirely), which used to read as "dead" and got
        # other users' live runs reaped as crashed. Ask the kernel instead:
        # kill(pid, 0) says EPERM for a live process we can't see into.
    return _pid_exists(int(pid))


def _pid_exists(pid: int) -> bool:
    """Non-destructive "does this PID exist" probe.

    POSIX uses ``os.kill(pid, 0)`` — the signal-0 no-op that just checks
    permission/existence. **Windows must not**: CPython maps ``os.kill(pid, sig)``
    for any ``sig`` other than ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT`` to
    ``TerminateProcess(handle, sig)``, so ``os.kill(pid, 0)`` *kills* the process
    (with exit code 0) instead of probing it. That turned this liveness check —
    which runs against the ``pid`` of every ``running`` row, including the live
    console's own row and any in-flight runnable — into a process killer on
    Windows. There it probes with ``OpenProcess`` instead (Windows has no
    ``/proc``, so ``proc_start`` is always empty and this branch is always taken)."""
    if _on_windows():
        return _win_pid_exists(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except (OSError, ValueError):
        return False


def _on_windows() -> bool:
    """True on Windows, where ``os.kill`` terminates rather than probes — so
    liveness must go through :func:`_win_pid_exists`. Indirected (not an inline
    ``os.name`` test) so it is patchable without mutating the global ``os.name``
    (which would flip ``pathlib`` to ``WindowsPath`` and break unrelated code)."""
    return os.name == "nt"


def _win_pid_exists(pid: int) -> bool:
    """Windows PID existence via ``OpenProcess`` — never ``os.kill`` (which on
    Windows terminates the process). True when the process exists (including an
    access-denied open — it exists, owned by another user), False only when it
    demonstrably does not (``ERROR_INVALID_PARAMETER``). Fails **safe**: any probe
    error assumes alive, so a liveness check can never wrongly drop a live run nor
    (the original defect) act destructively; an unreapable orphan is instead
    bounded by the max-age ceiling in :func:`_reap`."""
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_INVALID_PARAMETER = 87
        STILL_ACTIVE = 259

        # ``WinDLL`` / ``get_last_error`` are Windows-only (absent from the Linux
        # typeshed), so reach them via getattr to keep mypy clean on every host.
        win_dll = getattr(ctypes, "WinDLL", None)
        get_last_error = getattr(ctypes, "get_last_error", None)
        if win_dll is None or get_last_error is None:
            return True  # not really Windows — assume alive (never destructive)

        k32 = win_dll("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)

        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # No such process → dead; any other failure (e.g. access denied) means
            # the process exists, so assume alive.
            return bool(get_last_error() != ERROR_INVALID_PARAMETER)
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return bool(code.value == STILL_ACTIVE)
            return True
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return True  # never destructive, never wrongly-dead


# ── connection (chmod-before-WAL, WAL, busy_timeout, incremental auto-vacuum) ────


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection | None:
    """Open the DB best-effort, or ``None`` on any failure.

    A read-only open uses ``mode=ro`` (validated to see uncheckpointed WAL rows
    across processes — docs §12). A read-write open, on first create, ``chmod``s
    the file group-writable **before** entering WAL so the sidecars inherit it
    (umask can't add group-write); it also heals an owned, non-group-writable DB."""
    try:
        if _unsafe_dir(path.parent):
            return None
        if readonly:
            if not path.exists():
                return None
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_MS / 1000)
            con.row_factory = sqlite3.Row
            return con

        fresh = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000)
        con.row_factory = sqlite3.Row
        # Group-writable BEFORE WAL so SQLite creates -wal/-shm group-writable too
        # (docs §12). Best-effort: only works on a file this process owns, which is
        # the first (creating) writer; a later different-user writer relies on the
        # first having set it + the setgid dir for group ownership.
        _ensure_group_writable(path, fresh)
        if fresh:
            _chown_to_dir_owner(path)
        if fresh:
            con.execute("PRAGMA auto_vacuum=INCREMENTAL")  # before first table write
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        con.executescript(_DDL)  # CREATE TABLE IF NOT EXISTS — fresh DBs get every column
        _migrate(con)  # add columns an older DB predates (v1 → v2 → v3)
        _migrate_sync(con)  # v3 → v4: change sequence + tombstones + triggers
        con.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        return con
    except sqlite3.Error:
        return None
    except OSError:
        return None


def _migrate(con: sqlite3.Connection) -> None:
    """Apply additive column adds an existing DB predates — the forward/backward-
    compatible half of a schema bump. v1 → v2 (§8.1): ``log_pruned_at`` /
    ``log_pruned_by``; v2 → v3: ``label``; v4 → v5: ``events`` / ``exception``; v5 → v6: ``os_user`` / ``source`` / ``exec_uid``. ``ALTER TABLE ADD COLUMN`` is metadata-only (no table
    rewrite); a fresh DB already has them via ``_DDL`` so this is a no-op there.
    Best-effort — a migration failure never blocks the write (the DB is a derived
    index; worst case it is rebuilt via :func:`reindex`)."""
    with contextlib.suppress(sqlite3.Error):
        have = {r[1] for r in con.execute("PRAGMA table_info(runs)").fetchall()}
        for col in ("log_pruned_at", "log_pruned_by", "label", "events", "exception", "os_user", "source"):
            if col not in have:
                con.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
        if "exec_uid" not in have:
            con.execute("ALTER TABLE runs ADD COLUMN exec_uid INTEGER")
        # Here, not in _DDL: on a pre-v3 DB the column only exists after the ALTER.
        con.execute("CREATE INDEX IF NOT EXISTS runs_label ON runs (label)")


# ── v4 change sequence (docs/design/history-incremental-pull.md §3) ─────────────

# How long a delete's tombstone is kept for readers to learn about it, and a hard
# cap on how many are kept. Dropping tombstones raises ``floor``: a reader whose
# cursor is below it may have missed deletes, so it is told to ``reset``.
_TOMBSTONE_DAYS = 30
_TOMBSTONE_CAP = 50000
_SYNC_TRIGGERS = ("runs_seq_ins", "runs_seq_upd", "runs_seq_del")

# Every write to ``runs`` takes the next ``seq`` (inside the write transaction —
# SQLite serializes writers, so a reader never sees N+1 committed while N is still
# in flight). An insert also clears a tombstone for the same run_id (a pruned row
# restored by reindex), so a pull never carries both "deleted" and the row.
# ``WHEN NEW.seq IS OLD.seq`` skips the trigger's own seq write should
# ``recursive_triggers`` ever be on.
_SYNC_DDL = """
CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value);
CREATE TABLE IF NOT EXISTS runs_tombstones (
  run_id     TEXT PRIMARY KEY,
  seq        INTEGER NOT NULL,
  deleted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_seq ON runs (seq);
CREATE INDEX IF NOT EXISTS runs_tombstones_seq ON runs_tombstones (seq);
"""

# One statement each (not an executescript — that would commit the migration's
# BEGIN IMMEDIATE part-way through).
_SYNC_TRIGGERS_DDL = (
    """CREATE TRIGGER IF NOT EXISTS runs_seq_ins AFTER INSERT ON runs BEGIN
  UPDATE sync_meta SET value = value + 1 WHERE key = 'seq';
  UPDATE runs SET seq = (SELECT value FROM sync_meta WHERE key = 'seq') WHERE rowid = NEW.rowid;
  DELETE FROM runs_tombstones WHERE run_id = NEW.run_id;
END""",
    """CREATE TRIGGER IF NOT EXISTS runs_seq_upd AFTER UPDATE ON runs WHEN NEW.seq IS OLD.seq BEGIN
  UPDATE sync_meta SET value = value + 1 WHERE key = 'seq';
  UPDATE runs SET seq = (SELECT value FROM sync_meta WHERE key = 'seq') WHERE rowid = NEW.rowid;
END""",
    """CREATE TRIGGER IF NOT EXISTS runs_seq_del AFTER DELETE ON runs BEGIN
  UPDATE sync_meta SET value = value + 1 WHERE key = 'seq';
  INSERT OR REPLACE INTO runs_tombstones (run_id, seq, deleted_at)
    VALUES (OLD.run_id, (SELECT value FROM sync_meta WHERE key = 'seq'), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
END""",
)


def _has_sync(con: sqlite3.Connection) -> bool:
    """True when the DB carries the v4 change sequence — by structure (``sync_meta``
    exists and ``runs`` has ``seq``), since user_version can't be trusted."""
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_meta'").fetchone():
            return False
        return any(r[1] == "seq" for r in con.execute("PRAGMA table_info(runs)").fetchall())
    except sqlite3.Error:
        return False


def _sync_ready(con: sqlite3.Connection) -> bool:
    """The whole v4 setup is in place: tables, all three triggers, and no row
    still waiting for a backfilled ``seq``."""
    if not _has_sync(con):
        return False
    try:
        n = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?)",
            _SYNC_TRIGGERS,
        ).fetchone()[0]
        if n != len(_SYNC_TRIGGERS):
            return False
        if con.execute("SELECT count(*) FROM sync_meta WHERE key IN ('db_id','seq','floor')").fetchone()[0] != 3:
            return False
        return con.execute("SELECT 1 FROM runs WHERE seq IS NULL LIMIT 1").fetchone() is None
    except sqlite3.Error:
        return False


def _migrate_sync(con: sqlite3.Connection) -> None:
    """v3 → v4: add ``seq``, ``sync_meta`` (``db_id``/``seq``/``floor``),
    ``runs_tombstones`` and the triggers; backfill ``seq`` for existing rows in
    rowid order. One ``BEGIN IMMEDIATE`` transaction, so two writers migrating at
    once can't hand out the same seq twice. A no-op (one cheap check) once done.
    Best-effort — a failure leaves the DB a working v3 index."""
    if _sync_ready(con):
        return
    import uuid

    with contextlib.suppress(sqlite3.Error):
        if con.in_transaction:
            con.commit()
        con.execute("BEGIN IMMEDIATE")
        try:
            have = {r[1] for r in con.execute("PRAGMA table_info(runs)").fetchall()}
            if "seq" not in have:
                con.execute("ALTER TABLE runs ADD COLUMN seq INTEGER")
            for stmt in _SYNC_DDL.split(";"):
                if stmt.strip():
                    con.execute(stmt)
            con.execute("INSERT OR IGNORE INTO sync_meta (key, value) VALUES ('db_id', ?)", (uuid.uuid4().hex,))
            con.execute("INSERT OR IGNORE INTO sync_meta (key, value) VALUES ('seq', 0)")
            con.execute("INSERT OR IGNORE INTO sync_meta (key, value) VALUES ('floor', 0)")
            pending = [r[0] for r in con.execute("SELECT rowid FROM runs WHERE seq IS NULL ORDER BY rowid").fetchall()]
            if pending:
                base = int(con.execute("SELECT value FROM sync_meta WHERE key='seq'").fetchone()[0] or 0)
                con.executemany("UPDATE runs SET seq=? WHERE rowid=?", [(base + i, rid) for i, rid in enumerate(pending, 1)])
                con.execute("UPDATE sync_meta SET value=? WHERE key='seq'", (base + len(pending),))
            for stmt in _SYNC_TRIGGERS_DDL:
                con.execute(stmt)
            con.commit()
        except sqlite3.Error:
            con.rollback()
            raise


def _trim_tombstones(con: sqlite3.Connection, days: int = _TOMBSTONE_DAYS, cap: int = _TOMBSTONE_CAP) -> None:
    """Drop tombstones older than ``days`` (and beyond ``cap``, oldest first) and
    raise ``floor`` to the highest seq dropped, so a reader whose cursor predates
    them is sent a ``reset`` instead of silently missing those deletes."""
    if not _has_sync(con):
        return
    with contextlib.suppress(sqlite3.Error):
        top = con.execute(
            "SELECT max(seq) FROM runs_tombstones WHERE deleted_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
            (f"-{int(days)} days",),
        ).fetchone()[0]
        over = con.execute("SELECT seq FROM runs_tombstones ORDER BY seq DESC LIMIT 1 OFFSET ?", (int(cap),)).fetchone()
        if over is not None:
            top = max(top or 0, over[0])
        if top:
            con.execute("DELETE FROM runs_tombstones WHERE seq <= ?", (top,))
            con.execute("UPDATE sync_meta SET value = max(value, ?) WHERE key='floor'", (top,))


def _unsafe_dir(d: Path) -> str:
    """Why an index must never live in ``d`` — ``""`` when it's fine.

    A world-writable or sticky directory lets any local user plant the DB's
    ``-wal``/``-shm``/``-journal`` sidecars (or a whole fake DB), which can corrupt
    or forge the index; a read-only reader in such a dir can even create sidecars
    the owner then can't write. So the index refuses those dirs outright — runs
    still log, they're just not indexed there. POSIX only."""
    if not hasattr(os, "geteuid"):
        return ""
    try:
        mode = d.stat().st_mode
    except OSError:
        return ""
    if mode & (stat.S_IWOTH | stat.S_ISVTX):
        return f"refusing to keep a runs index in {d}: it is world-writable or sticky"
    return ""


def _chown_to_dir_owner(path: Path) -> None:
    """A DB created by root in a directory owned by someone else is handed to that
    owner (before WAL, so SQLite gives the -wal/-shm sidecars the same owner).
    Otherwise a root-run runnable that runs first leaves a root-owned index every
    other account — the venv's own service account included — silently can't
    write. Best-effort; a no-op unless running as root."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    with contextlib.suppress(OSError):
        st = path.parent.stat()
        if st.st_uid != 0:
            os.chown(path, st.st_uid, st.st_gid)


def _ensure_group_writable(path: Path, fresh: bool) -> None:
    """chmod the DB 0664 on create, or heal an owned DB that isn't group-writable
    yet (e.g. written by a pre-index runspec). Best-effort; a file we don't own
    can't be chmod'd and doesn't need to be (the creator already did).

    POSIX-only: there is no group-write bit or ``os.getuid`` on Windows, so this
    is a no-op there. (Without the guard ``os.getuid()`` raised ``AttributeError``
    — not caught by ``suppress(OSError)`` — which broke ``begin_run`` on a fresh
    Windows DB and dropped the start row.)"""
    if not hasattr(os, "getuid"):
        return
    with contextlib.suppress(OSError):
        st = path.stat()
        # group-write bit missing (or a fresh file), and we own it → make it 0664
        if (fresh or not (st.st_mode & 0o020)) and st.st_uid == os.getuid():
            os.chmod(path, 0o664)


# ── write path (best-effort, never raises) ──────────────────────────────────────


def begin_run(log_dir: Path, row: dict[str, Any]) -> None:
    """Insert the ``running`` row at run start. Best-effort; a failure is swallowed
    (the run proceeds unindexed — the finalize UPSERT can still create the row)."""
    with contextlib.suppress(Exception):
        con = _connect(db_path(log_dir))
        if con is None:
            return
        try:
            _upsert(con, _normalise_row(row))
            con.commit()
        finally:
            con.close()


def finish_run(
    log_dir: Path,
    run_id: str,
    *,
    exit_code: int,
    duration_ms: int,
    status: str,
    cfg: dict[str, Any] | None = None,
    runnable: str = "",
    command: str = "",
    events: dict[str, int] | None = None,
    exception: Any = None,
    sweep: bool = True,
) -> None:
    """Finalize a row (``completed``/``failed`` + exit code + duration) via UPSERT
    keyed on ``run_id`` — correct whether or not the start row landed. Then run the
    opportunistic maintenance (reap orphans + ring-prune) on the same connection,
    since the run's work is already done so exit-path latency is harmless.
    Best-effort throughout.

    ``runnable``/``command`` are recorded on the **fallback INSERT** (when no start
    row exists — begin_run lost its race, or the index was unwritable/contended at
    start). Without them that row lands nameless, and the console History read maps
    a DB row's ``runnable`` with no filename fallback, so it renders as
    ``<venv>/`` with a blank name.

    ``events`` / ``exception`` are the run_summary's log-record counts and uncaught
    exception (v5). Omitted (``runspec exec`` finalizing a child that already
    wrote its own), they're left as they are."""
    with contextlib.suppress(Exception):
        con = _connect(db_path(log_dir))
        if con is None:
            return
        try:
            end_ts = _now_iso()
            ev, exc = _events_json(events), _exception_json(exception)
            cur = con.execute(
                "UPDATE runs SET status=?, exit_code=?, end_ts=?, duration_ms=?, events=COALESCE(?, events), exception=COALESCE(?, exception) WHERE run_id=?",
                (status, exit_code, end_ts, duration_ms, ev, exc, run_id),
            )
            # This statement's own count — not con.total_changes, which also counts
            # the migration's writes (and trigger writes) on this connection.
            if (cur.rowcount or 0) <= 0:
                # No start row (begin_run lost its race / was disabled / the index
                # was unwritable at start) — record a finished row directly so
                # history is still complete, carrying the runnable name + command
                # so the row is never nameless.
                con.execute(
                    "INSERT OR IGNORE INTO runs (run_id, runnable, command, status, exit_code, start_ts, end_ts, duration_ms, events, exception) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (run_id, runnable or "", command or "", status, exit_code, end_ts, end_ts, duration_ms, ev, exc),
                )
            con.commit()
            _maintain(con, cfg or {})
            con.commit()
        finally:
            con.close()
    # The owner's finalize also drains the inbox (throttled + time-boxed; a no-op
    # for anyone else and where there's no inbox). After the close, so the sweep's
    # own connection doesn't contend with ours.
    if sweep:
        with contextlib.suppress(Exception):
            from . import inbox

            inbox.maybe_sweep(log_dir, cfg)


def _events_json(events: Any) -> str | None:
    """``{level: count}`` → JSON (non-zero counts). A run that logged nothing is
    ``"{}"`` — NULL is reserved for *unknown* (a runspec that predates v5)."""
    if not isinstance(events, dict):
        return None
    clean = {str(k): int(v) for k, v in events.items() if isinstance(v, int) and not isinstance(v, bool) and v}
    return json.dumps(clean, sort_keys=True)


def _exception_json(exc: Any) -> str | None:
    """A run_summary ``exception`` (``{type, message, traceback}`` or a string) →
    JSON ``{type, message}``, message capped. The traceback stays in the log."""
    if not exc:
        return None
    if isinstance(exc, dict):
        etype, msg = str(exc.get("type") or ""), str(exc.get("message") or "")
    else:
        etype, msg = "", str(exc)
    return json.dumps({"type": etype, "message": msg[:_EXCEPTION_MESSAGE_MAX]})


def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {c: row.get(c) for c in _COLUMNS}
    out["label"] = row.get("label") or None
    out["os_user"] = row.get("os_user") or None
    out["source"] = row.get("source") or None
    out["exec_uid"] = _int_or_none(row.get("exec_uid"))
    out["events"] = _events_json(row.get("events"))
    out["exception"] = _exception_json(row.get("exception"))
    out["run_id"] = row.get("run_id") or ""
    out["runnable"] = row.get("runnable") or ""
    out["status"] = row.get("status") or "running"
    out["start_ts"] = row.get("start_ts") or _now_iso()
    if isinstance(out["agent"], bool):
        out["agent"] = int(out["agent"])
    if out.get("args") is not None and not isinstance(out["args"], str):
        with contextlib.suppress(Exception):
            out["args"] = json.dumps(out["args"], default=str)
    return out


def _upsert(con: sqlite3.Connection, row: dict[str, Any]) -> None:
    """INSERT or overwrite a row by ``run_id``. ``label``, ``events`` and
    ``exception`` (and v6 ``os_user`` / ``source`` / ``exec_uid``) are written on
    insert but only ever *kept* on conflict: under
    ``runspec exec`` the wrapper writes the labelled row first and the child's own
    begin/finish upserts (which don't know the label) must not clear it — and a
    begin that races a finish must not clear the finish's events."""
    kept = ("label", "events", "exception", "os_user", "source", "exec_uid")
    cols = ", ".join((*_COLUMNS, *kept))
    placeholders = ", ".join("?" for _ in range(len(_COLUMNS) + len(kept)))
    updates = ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c != "run_id")
    updates += "".join(f", {c}=COALESCE(excluded.{c}, runs.{c})" for c in kept)
    con.execute(
        f"INSERT INTO runs ({cols}) VALUES ({placeholders}) ON CONFLICT(run_id) DO UPDATE SET {updates}",
        (*(row[c] for c in _COLUMNS), *(row.get(c) if row.get(c) not in ("", None) else None for c in kept)),
    )


# ── maintenance: reap orphans + ring-prune ──────────────────────────────────────


def _maintain(con: sqlite3.Connection, cfg: dict[str, Any]) -> None:
    _reap(con, _int(cfg.get("max_run_hours"), 24))
    _prune(con, _int(cfg.get("retention_days"), 90), _int(cfg.get("max_rows"), 50000))
    _trim_tombstones(con)
    with contextlib.suppress(sqlite3.Error):
        con.execute("PRAGMA incremental_vacuum")


def _reap(con: sqlite3.Connection, max_run_hours: int) -> None:
    """Mark dead ``running`` rows ``crashed`` — pid not alive on this host, or older
    than the age ceiling. Cross-host rows are left to their own host's reaper."""
    host = _hostname()
    with contextlib.suppress(sqlite3.Error):
        rows = con.execute(
            "SELECT run_id, pid, proc_start FROM runs WHERE status='running' AND host=?",
            (host,),
        ).fetchall()
        dead = [r["run_id"] for r in rows if not _pid_alive(r["pid"], r["proc_start"])]
        for rid in dead:
            con.execute("UPDATE runs SET status='crashed' WHERE run_id=?", (rid,))
    if max_run_hours > 0:
        cutoff = _iso_hours_ago(max_run_hours)
        with contextlib.suppress(sqlite3.Error):
            con.execute(
                "UPDATE runs SET status='crashed' WHERE status='running' AND start_ts < ?",
                (cutoff,),
            )


def _prune(con: sqlite3.Connection, retention_days: int, max_rows: int) -> None:
    """Ring-buffer trim of finished rows: drop those older than the age bound, then
    any beyond the row cap (oldest first). ``running`` rows are never pruned."""
    if retention_days > 0:
        cutoff = _iso_hours_ago(retention_days * 24)
        with contextlib.suppress(sqlite3.Error):
            con.execute("DELETE FROM runs WHERE status != 'running' AND start_ts < ?", (cutoff,))
    if max_rows > 0:
        with contextlib.suppress(sqlite3.Error):
            con.execute(
                "DELETE FROM runs WHERE run_id IN (  SELECT run_id FROM runs WHERE status != 'running'   ORDER BY start_ts DESC LIMIT -1 OFFSET ?)",
                (max_rows,),
            )


def reap_now(log_dir: Path, cfg: dict[str, Any] | None = None) -> int:
    """Force an orphan sweep (the ``runspec history reap`` verb). Returns the number
    of rows reaped, or 0 on any failure."""
    cfg = cfg or {}
    try:
        con = _connect(db_path(log_dir))
        if con is None:
            return 0
        try:
            before: int = con.execute("SELECT count(*) FROM runs WHERE status='crashed'").fetchone()[0]
            _reap(con, _int(cfg.get("max_run_hours"), 24))
            con.commit()
            after: int = con.execute("SELECT count(*) FROM runs WHERE status='crashed'").fetchone()[0]
            return max(0, after - before)
        finally:
            con.close()
    except Exception:
        return 0


def prune_now(log_dir: Path, *, retention_days: int, max_rows: int) -> int:
    """Explicit prune (the ``runspec history prune`` verb). Returns rows deleted."""
    try:
        con = _connect(db_path(log_dir))
        if con is None:
            return 0
        try:
            before: int = con.execute("SELECT count(*) FROM runs").fetchone()[0]
            _prune(con, retention_days, max_rows)
            con.commit()
            with contextlib.suppress(sqlite3.Error):
                con.execute("PRAGMA incremental_vacuum")
                con.commit()
            after: int = con.execute("SELECT count(*) FROM runs").fetchone()[0]
            return max(0, before - after)
        finally:
            con.close()
    except Exception:
        return 0


# ── read path (mode=ro; used by the console via {venv}/bin/python -c) ────────────


def list_runs(
    log_dir: Path,
    *,
    since: str | None = None,
    status: str | None = None,
    runnable: str | None = None,
    limit: int = 100,
    running_only: bool = False,
    live: bool = True,
    label: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-first rows for the History/Logs read. ``since`` is an ISO date/time
    lower bound (the real ``WHERE`` the date window becomes). ``live`` liveness-
    filters ``running`` rows on this host so an un-reaped orphan never reads as
    running. Best-effort → ``[]`` on failure."""
    try:
        con = _connect(db_path(log_dir), readonly=True)
        if con is None:
            return []
        try:
            clauses, params = [], []
            if running_only:
                clauses.append("status='running'")
            if since:
                clauses.append("start_ts >= ?")
                params.append(since)
            if status:
                clauses.append("status=?")
                params.append(status)
            if runnable:
                clauses.append("runnable=?")
                params.append(runnable)
            if label:
                clauses.append("label=?")
                params.append(label)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = con.execute(
                f"SELECT * FROM runs{where} ORDER BY start_ts DESC LIMIT ?",
                (*params, max(1, int(limit))),
            ).fetchall()
            out = [dict(r) for r in rows]
            if live:
                out = _liveness_filter(out)
                if running_only:
                    # A "what's running" view shows only genuinely-live runs — a
                    # dead-pid row was just downgraded to crashed, so drop it.
                    out = [r for r in out if r.get("status") == "running"]
            return out
        finally:
            con.close()
    except Exception:
        return []


def sync_state(con: sqlite3.Connection) -> dict[str, Any] | None:
    """``{"db_id", "seq", "floor"}`` from ``sync_meta``, or ``None`` for a pre-v4 DB."""
    if not _has_sync(con):
        return None
    meta = {r[0]: r[1] for r in con.execute("SELECT key, value FROM sync_meta").fetchall()}
    if "db_id" not in meta:
        return None
    return {"db_id": str(meta["db_id"]), "seq": int(meta.get("seq") or 0), "floor": int(meta.get("floor") or 0)}


def changes(
    con: sqlite3.Connection,
    *,
    db_id: str | None,
    seq: int | None,
    limit: int = 2000,
    window_start: str | None = None,
) -> dict[str, Any]:
    """The incremental-pull contract (docs/design/history-incremental-pull.md §4)
    over an open connection — everything in one read transaction.

    Returns ``{"mode", "db_id", "seq", "rows", "deleted", "more"}``:

    * ``delta`` — the cursor ``(db_id, seq)`` is valid: ``rows`` changed after it
      (finished *and* running, oldest change first), ``deleted`` the run_ids
      removed after it.
    * ``reset`` — no cursor, a different ``db_id`` (the DB was recreated), or a
      cursor below ``floor`` (tombstones it needed are gone): drop what you hold for
      this DB; ``rows`` is a snapshot (``start_ts >= window_start``).
    * ``legacy`` — the DB predates v4 (no change sequence); read it directly.

    ``seq`` in the result is the cursor to store. ``more`` means the page was capped
    at ``limit``: apply it and pull again from the returned cursor. ``window_start``
    also filters deltas — a reader that only keeps a window never needs changes to
    rows older than it."""
    limit = max(1, int(limit))
    con.execute("BEGIN")  # one read snapshot for meta + rows + tombstones
    try:
        meta = sync_state(con)
        if meta is None:
            return {"mode": "legacy", "db_id": None, "seq": 0, "rows": [], "deleted": [], "more": False}
        valid = seq is not None and db_id == meta["db_id"] and meta["floor"] <= int(seq) <= meta["seq"]
        after = int(seq) if valid and seq is not None else 0
        where = "seq > ?"
        params: list[Any] = [after]
        if window_start:
            where += " AND start_ts >= ?"
            params.append(window_start)
        rows = [dict(r) for r in con.execute(f"SELECT * FROM runs WHERE {where} ORDER BY seq LIMIT ?", (*params, limit + 1)).fetchall()]
        tombs: list[tuple[str, int]] = []
        if valid:
            tombs = [(r[0], r[1]) for r in con.execute("SELECT run_id, seq FROM runs_tombstones WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit + 1)).fetchall()]
        merged: list[tuple[int, Any]] = sorted([(r["seq"], r) for r in rows] + [(t[1], t[0]) for t in tombs], key=lambda x: x[0])
        more = len(merged) > limit
        page = merged[:limit]
        cursor = page[-1][0] if more else meta["seq"]
        return {
            "mode": "delta" if valid else "reset",
            "db_id": meta["db_id"],
            "seq": cursor,
            "rows": [item for _, item in page if isinstance(item, dict)],
            "deleted": [item for _, item in page if isinstance(item, str)],
            "more": more,
        }
    finally:
        con.rollback()


def changes_since(
    log_dir: Path,
    *,
    db_id: str | None = None,
    seq: int | None = None,
    limit: int = 2000,
    window_start: str | None = None,
) -> dict[str, Any]:
    """:func:`changes` for one dir's index (read-only), plus ``mode="none"`` when the
    dir has no index. Never raises — an unreadable index reports ``error``."""
    path = db_path(log_dir)
    if not path.exists():
        return {"mode": "none", "db_id": None, "seq": 0, "rows": [], "deleted": [], "more": False}
    try:
        con = _connect(path, readonly=True)
        if con is None:
            return {"mode": "none", "db_id": None, "seq": 0, "rows": [], "deleted": [], "more": False, "error": "could not open the index"}
        try:
            return changes(con, db_id=db_id, seq=seq, limit=limit, window_start=window_start)
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 — a read, never fatal
        return {"mode": "none", "db_id": None, "seq": 0, "rows": [], "deleted": [], "more": False, "error": f"{type(exc).__name__}: {exc}"}


def _liveness_filter(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Downgrade ``running`` rows on *this* host whose process is dead to
    ``crashed`` in the returned view (read-only — the DB itself is healed by the
    reaper). Rows for other hosts are left as-is (their host reaps them)."""
    host = _hostname()
    for r in rows:
        if r.get("status") == "running" and r.get("host") == host and not _pid_alive(r.get("pid"), r.get("proc_start")):
            r["status"] = "crashed"
    return rows


# ── `runspec exec` support (docs/design/runspec-exec.md §3.3) ───────────────────


def get_run(log_dir: Path, run_id: str) -> dict[str, Any] | None:
    """One row by ``run_id`` (read-only), or ``None`` when absent / unreadable."""
    try:
        con = _connect(db_path(log_dir), readonly=True)
        if con is None:
            return None
        try:
            r = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return dict(r) if r else None
        finally:
            con.close()
    except Exception:
        return None


def adopt_child_row(
    log_dir: Path,
    wrapper_run_id: str,
    *,
    runnable: str,
    since_ts: str,
    label: str | None,
) -> str | None:
    """Merge a pre-0.62 child's row into the wrapper's run.

    An older runspec ignores ``RUNSPEC_RUN_ID`` and records the run under its own
    fresh id, which would show as a second History row. When **exactly one** such
    row exists — same runnable, this host, started at/after ``since_ts``, no label,
    not the wrapper's — stamp the wrapper's label on it and delete the wrapper's
    placeholder so the child's row (with its args, command and log file) is the
    single record. Returns the adopted ``run_id``, or ``None`` when there is
    nothing (or nothing unambiguous) to adopt. Best-effort."""
    try:
        con = _connect(db_path(log_dir))
        if con is None:
            return None
        try:
            rows = con.execute(
                "SELECT run_id FROM runs WHERE runnable=? AND host=? AND start_ts >= ? AND run_id != ? AND label IS NULL",
                (runnable, _hostname(), since_ts, wrapper_run_id),
            ).fetchall()
            if len(rows) != 1:
                return None
            child_id: str = rows[0]["run_id"]
            con.execute("UPDATE runs SET label=? WHERE run_id=?", (label, child_id))
            con.execute("DELETE FROM runs WHERE run_id=?", (wrapper_run_id,))
            con.commit()
            return child_id
        finally:
            con.close()
    except Exception:
        return None


def set_log_file(log_dir: Path, run_id: str, log_file: str) -> None:
    """Point a row at its log file — used by ``runspec exec`` when it created the
    run's log file itself (the child wrote none). Best-effort."""
    with contextlib.suppress(Exception):
        con = _connect(db_path(log_dir))
        if con is None:
            return
        try:
            con.execute("UPDATE runs SET log_file=? WHERE run_id=? AND log_file IS NULL", (log_file, run_id))
            con.commit()
        finally:
            con.close()


# ── the inbox (docs/design/venv-logs-inbox.md §6, §8) ───────────────────────────

# A swept row may only *fill* these on a direct row it's bound to — never the
# outcome (status / exit code / duration), which the direct writer owns.
_FILL_ONLY = ("command", "user", "user_target", "agent", "trigger", "skill", "autonomy", "args", "label", "events", "exception", "log_file")


def inbox_binding(con: sqlite3.Connection, run_id: str, *, os_user: str, uid: int, row: dict[str, Any] | None = None) -> str:
    """Whether a run swept from an inbox file owned by ``uid`` (``os_user``) may
    write the index row for ``run_id`` — the binding table. Read-only.

    ``insert`` (no row) · ``update`` (an inbox row by the same account) ·
    ``outcome`` (a direct row whose ``exec_uid`` is this account — a
    ``runspec exec`` wrapper in the inbox whose child ran as the owner: take the
    observed outcome) · ``fill`` (a direct row whose ``run_as`` resolves to this
    account — a direct wrapper whose child ran in the inbox: fill empty columns
    only) · ``collision`` (anything else: one account's file never rewrites
    another's row)."""
    from . import inbox

    cur = con.execute("SELECT source, os_user, run_as, exec_uid FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if cur is None:
        return "insert"
    if cur["source"] == "inbox":
        return "update" if cur["os_user"] == os_user else "collision"
    if cur["exec_uid"] is not None and _int(cur["exec_uid"], -1) == uid and (row is None or row.get("exec")):
        return "outcome"
    if inbox.uid_of(cur["run_as"]) == uid:
        return "fill"
    return "collision"


def apply_inbox_row(con: sqlite3.Connection, row: dict[str, Any], *, os_user: str, uid: int, decision: str | None = None) -> str:
    """Write a swept row per the binding table (:func:`inbox_binding`). Returns
    the decision applied. The caller commits."""
    decision = decision or inbox_binding(con, row["run_id"], os_user=os_user, uid=uid, row=row)
    if decision in ("insert", "update"):
        _upsert(con, _normalise_row({**row, "source": "inbox", "os_user": os_user}))
    elif decision == "outcome":
        ex = row.get("exec") or {}
        con.execute(
            "UPDATE runs SET status=?, exit_code=?, duration_ms=COALESCE(?, duration_ms), end_ts=COALESCE(?, end_ts),"
            " label=COALESCE(label, ?), trigger=COALESCE(NULLIF(trigger, ''), ?) WHERE run_id=?",
            (ex.get("status") or "completed", ex.get("exit_code"), ex.get("duration_ms"), ex.get("end_ts"), row.get("label") or None, row.get("trigger") or None, row["run_id"]),
        )
    elif decision == "fill":
        norm = _normalise_row(row)
        sets = ", ".join(f"{c}=COALESCE(NULLIF({c}, ''), ?)" for c in _FILL_ONLY)
        con.execute(f"UPDATE runs SET {sets} WHERE run_id=?", (*(norm.get(c) for c in _FILL_ONLY), row["run_id"]))
    return decision


def list_runs_with_inbox(
    log_dir: Path,
    *,
    since: str | None = None,
    status: str | None = None,
    runnable: str | None = None,
    limit: int = 100,
    running_only: bool = False,
    live: bool = True,
    label: str | None = None,
    max_run_hours: int = 24,
) -> list[dict[str, Any]]:
    """:func:`list_runs` plus the runs still waiting in the inbox — the reader
    contract. The inbox is read **before** the index, then deduped by ``run_id``
    with the index winning: a run swept between the two reads is seen once, not
    zero times. Inbox rows carry ``pending=True``. Never raises."""
    pending: list[dict[str, Any]] = []
    with contextlib.suppress(Exception):
        from . import inbox

        pending = inbox.inbox_rows(Path(log_dir), max_run_hours=max_run_hours)
    indexed = list_runs(log_dir, since=since, status=status, runnable=runnable, limit=limit, running_only=running_only, live=live, label=label)
    have = {r.get("run_id") for r in indexed}
    extra = []
    for r in pending:
        if r.get("run_id") in have:
            continue
        if running_only and r.get("status") != "running":
            continue
        if since and str(r.get("start_ts") or "") < since:
            continue
        if status and r.get("status") != status:
            continue
        if runnable and r.get("runnable") != runnable:
            continue
        if label and r.get("label") != label:
            continue
        extra.append(_normalise_row(r) | {"pending": True, "log_pruned_at": None, "log_pruned_by": None})
    rows = indexed + extra
    rows.sort(key=lambda r: r.get("start_ts") or "", reverse=True)
    return rows[: max(1, int(limit))]


def reindex_rows(log_dir: Path, rows: list[dict[str, Any]]) -> int:
    """Rebuild finished rows from parsed log files (:func:`runspec.inbox.row_from_records`
    — so ``runspec exec``'s observed exit codes, labels and skips survive a
    reindex, which the run_summary-only :func:`reindex` loses). Rows already in
    the index keep their attribution. Returns rows written, 0 on failure."""
    try:
        con = _connect(db_path(log_dir))
        if con is None:
            return 0
        try:
            n = 0
            for row in rows:
                if not row.get("run_id") or not row.get("finished") or not row.get("history_db", True):
                    continue
                _upsert(con, _normalise_row(row))
                n += 1
            con.commit()
            return n
        finally:
            con.close()
    except Exception:
        return 0


# ── maintenance introspection (the ``runspec history stats`` verb) ───────────────


def stats(log_dir: Path) -> dict[str, Any]:
    """A read-only health snapshot of one dir's index — what an operator (or the
    console's Logs tab) needs to decide whether to prune / reap / reindex.

    ``size_bytes`` counts the DB plus its ``-wal``/``-shm`` sidecars (the WAL can
    dwarf the main file between checkpoints). ``running`` is the raw count of
    ``running`` rows; ``stale_running`` is how many of those on *this* host have a
    dead process (what ``reap`` would crash). ``write_error`` is non-empty when the
    invoking user couldn't write the index (the "run as the venv owner" case).
    Never raises — a missing / unreadable DB reports ``exists``/``error``."""
    path = db_path(log_dir)
    out: dict[str, Any] = {
        "dir": str(log_dir),
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": 0,
        "rows": 0,
        "by_status": {},
        "oldest": None,
        "newest": None,
        "running": 0,
        "stale_running": 0,
        "log_pruned": 0,
        "schema_version": None,
        "sync": None,
        "write_error": write_error(log_dir),
        "inbox": None,
    }
    with contextlib.suppress(Exception):
        from . import inbox

        if inbox.supported():
            out["inbox"] = inbox.status(Path(log_dir))
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            out["size_bytes"] += Path(f"{path}{suffix}").stat().st_size
    if not out["exists"]:
        return out
    try:
        con = _connect(path, readonly=True)
        if con is None:
            out["error"] = "could not open the index read-only"
            return out
        try:
            out["schema_version"] = con.execute("PRAGMA user_version").fetchone()[0]
            with contextlib.suppress(sqlite3.Error):
                sync = sync_state(con)
                if sync is not None:
                    # By structure: an older runspec may have reset user_version.
                    cols = {r[1] for r in con.execute("PRAGMA table_info(runs)").fetchall()}
                    structural = (6 if "os_user" in cols else 5) if "events" in cols else 4
                    out["schema_version"] = max(int(out["schema_version"] or 0), structural)
                    sync["tombstones"] = con.execute("SELECT count(*) FROM runs_tombstones").fetchone()[0]
                    out["sync"] = sync
            by_status = {r[0]: r[1] for r in con.execute("SELECT status, count(*) FROM runs GROUP BY status")}
            out["by_status"] = by_status
            out["rows"] = sum(by_status.values())
            oldest, newest = con.execute("SELECT min(start_ts), max(start_ts) FROM runs").fetchone()
            out["oldest"], out["newest"] = oldest, newest
            with contextlib.suppress(sqlite3.Error):  # a v1 DB predates the column
                out["log_pruned"] = con.execute("SELECT count(*) FROM runs WHERE log_pruned_at IS NOT NULL").fetchone()[0]
            host = _hostname()
            running = con.execute("SELECT pid, proc_start, host FROM runs WHERE status='running'").fetchall()
            out["running"] = len(running)
            out["stale_running"] = sum(1 for r in running if r["host"] == host and not _pid_alive(r["pid"], r["proc_start"]))
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 — a snapshot, never fatal
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def write_error(log_dir: Path) -> str:
    """Why the invoking user can't write this dir's index, or ``""`` if it can.

    The write helpers are best-effort (they return 0 on failure), so a prune run
    by a user who can't write a service-account-owned index would otherwise
    report "0 rows pruned" — indistinguishable from "nothing to prune". The
    maintenance verbs check this first so the failure is named. SQLite needs the
    file writable and — to create the ``-wal``/``-shm`` sidecars — the dir too."""
    path = db_path(log_dir)
    unsafe = _unsafe_dir(log_dir)
    if unsafe:
        return unsafe
    target = path if path.exists() else log_dir
    if not target.exists():
        # No dir yet: writable if the nearest existing ancestor is (mkdir works).
        parent = log_dir.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        target = parent
    if not os.access(target, os.W_OK):
        return f"permission denied: {target} is not writable by this user{_owner_hint(target)}"
    if path.exists() and not os.access(log_dir, os.W_OK):
        return f"permission denied: {log_dir} is not writable by this user{_owner_hint(log_dir)}"
    return ""


def _owner_hint(p: Path) -> str:
    try:
        import pwd

        return f" (owned by {pwd.getpwuid(p.stat().st_uid).pw_name})"
    except Exception:  # noqa: BLE001 — Windows (no pwd) / unknown uid
        return ""


# ── reindex (rebuild the DB from the on-disk run_summary log records) ────────────


def reindex(log_dir: Path, records: list[dict[str, Any]]) -> int:
    """Rebuild finished rows from run_summary records (the log stays the source of
    truth). ``records`` are the parsed ``extra`` dicts of ``event=="run_summary"``
    lines, each with an injected ``ts``. Returns rows written, 0 on failure."""
    try:
        con = _connect(db_path(log_dir))
        if con is None:
            return 0
        try:
            n = 0
            for rec in records:
                run_id = rec.get("run_id")
                if not run_id:
                    continue
                exc = rec.get("exception")
                exit_code = rec.get("exit_code", 1 if exc else 0)
                start_ts = _summary_start_ts(rec)
                _upsert(
                    con,
                    _normalise_row(
                        {
                            "run_id": run_id,
                            "runnable": rec.get("runnable", ""),
                            "command": " ".join(rec.get("command_path") or []),
                            "status": "failed" if exc or exit_code else "completed",
                            "exit_code": exit_code,
                            "start_ts": start_ts,
                            "end_ts": rec.get("ts"),
                            "duration_ms": rec.get("duration_ms"),
                            "user": rec.get("user"),
                            "user_target": rec.get("user_target"),
                            "agent": rec.get("agent"),
                            "trigger": rec.get("trigger", ""),
                            "skill": rec.get("skill", ""),
                            "autonomy": rec.get("autonomy", ""),
                            "args": rec.get("invocation_args") or {},
                            "events": rec.get("events"),
                            "exception": exc,
                        }
                    ),
                )
                n += 1
            con.commit()
            return n
        finally:
            con.close()
    except Exception:
        return 0


def _summary_start_ts(rec: dict[str, Any]) -> str:
    """A run's start from its summary record: end ts minus duration when both are
    present (the record is written at the end), else the end ts."""
    end = rec.get("ts")
    dur = rec.get("duration_ms")
    if isinstance(end, str) and isinstance(dur, int):
        with contextlib.suppress(Exception):
            dt = datetime.fromisoformat(end)
            return (dt - _ms(dur)).isoformat()
    return end if isinstance(end, str) else _now_iso()


# ── prune provenance (§8.1): the log FILE's availability, not the run outcome ────


def mark_log_pruned(
    log_dir: Path,
    log_files: list[str] | tuple[str, ...],
    *,
    pruned_by: str,
    pruned_at: str | None = None,
) -> int:
    """Stamp every row whose ``log_file`` matches a just-deleted file basename with
    when (``log_pruned_at``, ISO-8601 UTC) and by whom (``log_pruned_by``, the OS
    user that ran the prune) its log was pruned — so the console can say "log pruned
    by X on Y" for a row that outlived its log file (a normal state: the DB is a
    bounded index, logs prune on their own schedule). An archive backing several
    ``run_id``s stamps **all** rows matching its basename.

    Best-effort → 0 on any failure, and it runs *after* the ``unlink`` so a locked/
    unwritable DB can never strand the delete (§7). Returns rows stamped."""
    names = [n for n in log_files if n]
    if not names:
        return 0
    ts = pruned_at or _now_iso()
    try:
        con = _connect(db_path(log_dir))
        if con is None:
            return 0
        try:
            n = 0
            for name in names:
                cur = con.execute(
                    "UPDATE runs SET log_pruned_at=?, log_pruned_by=? WHERE log_file=?",
                    (ts, pruned_by, name),
                )
                n += max(0, cur.rowcount or 0)
            con.commit()
            return n
        finally:
            con.close()
    except Exception:
        return 0


def repoint_logs(log_dir: Path, moves: dict[str, str]) -> int:
    """Repoint rows' ``log_file`` from an old basename to a new one — used by
    ``compact``, which folds per-run files into a dated archive: the content
    survives under a new name, so the row is **not** marked pruned (§8.1), just
    repointed, keeping both the per-log fetch and a future prune-stamp match working
    across the compaction. Best-effort → 0 on failure. Returns rows repointed."""
    pairs = [(new, old) for old, new in moves.items() if old and new and old != new]
    if not pairs:
        return 0
    try:
        con = _connect(db_path(log_dir))
        if con is None:
            return 0
        try:
            n = 0
            for new, old in pairs:
                cur = con.execute("UPDATE runs SET log_file=? WHERE log_file=?", (new, old))
                n += max(0, cur.rowcount or 0)
            con.commit()
            return n
        finally:
            con.close()
    except Exception:
        return 0


# ── small helpers ────────────────────────────────────────────────────────────────


def _hostname() -> str:
    import socket

    with contextlib.suppress(Exception):
        return socket.gethostname()
    return ""


def _int_or_none(v: Any) -> int | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any, default: int) -> int:
    try:
        n: int = int(v)
        return n
    except (TypeError, ValueError):
        return default


def _iso_hours_ago(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _ms(ms: int) -> timedelta:
    return timedelta(milliseconds=ms)

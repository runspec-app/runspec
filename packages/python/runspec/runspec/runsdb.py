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
_SCHEMA_VERSION = 3
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
  label         TEXT
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
        return _read_proc_start(int(pid)) == proc_start
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
            con.execute("PRAGMA auto_vacuum=INCREMENTAL")  # before first table write
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        con.executescript(_DDL)  # CREATE TABLE IF NOT EXISTS — fresh DBs get every column
        _migrate(con)  # add columns an older DB predates (v1 → v2)
        con.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        return con
    except sqlite3.Error:
        return None
    except OSError:
        return None


def _migrate(con: sqlite3.Connection) -> None:
    """Apply additive column adds an existing DB predates — the forward/backward-
    compatible half of a schema bump. v1 → v2 (§8.1): ``log_pruned_at`` /
    ``log_pruned_by``; v2 → v3: ``label``. ``ALTER TABLE ADD COLUMN`` is metadata-only (no table
    rewrite); a fresh DB already has them via ``_DDL`` so this is a no-op there.
    Best-effort — a migration failure never blocks the write (the DB is a derived
    index; worst case it is rebuilt via :func:`reindex`)."""
    with contextlib.suppress(sqlite3.Error):
        have = {r[1] for r in con.execute("PRAGMA table_info(runs)").fetchall()}
        for col in ("log_pruned_at", "log_pruned_by", "label"):
            if col not in have:
                con.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
        # Here, not in _DDL: on a pre-v3 DB the column only exists after the ALTER.
        con.execute("CREATE INDEX IF NOT EXISTS runs_label ON runs (label)")


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
    ``<venv>/`` with a blank name."""
    with contextlib.suppress(Exception):
        con = _connect(db_path(log_dir))
        if con is None:
            return
        try:
            end_ts = _now_iso()
            con.execute(
                "UPDATE runs SET status=?, exit_code=?, end_ts=?, duration_ms=? WHERE run_id=?",
                (status, exit_code, end_ts, duration_ms, run_id),
            )
            if con.total_changes == 0:
                # No start row (begin_run lost its race / was disabled / the index
                # was unwritable at start) — record a finished row directly so
                # history is still complete, carrying the runnable name + command
                # so the row is never nameless.
                con.execute(
                    "INSERT OR IGNORE INTO runs (run_id, runnable, command, status, exit_code, start_ts, end_ts, duration_ms) VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, runnable or "", command or "", status, exit_code, end_ts, end_ts, duration_ms),
                )
            con.commit()
            _maintain(con, cfg or {})
            con.commit()
        finally:
            con.close()


def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {c: row.get(c) for c in _COLUMNS}
    out["label"] = row.get("label") or None
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
    """INSERT or overwrite a row by ``run_id``. ``label`` is written on insert but
    only ever *kept* on conflict: under ``runspec exec`` the wrapper writes the
    labelled row first and the child's own begin/finish upserts (which don't know
    the label) must not clear it."""
    cols = ", ".join((*_COLUMNS, "label"))
    placeholders = ", ".join("?" for _ in range(len(_COLUMNS) + 1))
    updates = ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c != "run_id")
    updates += ", label=COALESCE(excluded.label, runs.label)"
    con.execute(
        f"INSERT INTO runs ({cols}) VALUES ({placeholders}) ON CONFLICT(run_id) DO UPDATE SET {updates}",
        (*(row[c] for c in _COLUMNS), row.get("label") or None),
    )


# ── maintenance: reap orphans + ring-prune ──────────────────────────────────────


def _maintain(con: sqlite3.Connection, cfg: dict[str, Any]) -> None:
    _reap(con, _int(cfg.get("max_run_hours"), 24))
    _prune(con, _int(cfg.get("retention_days"), 90), _int(cfg.get("max_rows"), 50000))
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
        "write_error": write_error(log_dir),
    }
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

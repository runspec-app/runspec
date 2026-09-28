"""history_cache.py — the console's local cache of remote hosts' run history.

The History tab reads remote hosts from here instead of waiting on an SSH read per
refresh: a background pull keeps each host current by fetching only what changed
in each venv's runs index since the last pull (core ``runspec`` ≥ 0.63.0, schema
v4 — ``runspec.runsdb.changes``). Design: ``docs/design/history-incremental-pull.md``.

**Machine-local, never synced, derived.** ``{app_dir}/history_cache.db``; a
corrupt or unreadable file is deleted and rebuilt, and nothing here is a source of
truth — the hosts are. The console never writes back.

Stores *mapped* History records (the ``HistoryRecord`` dicts the tab renders), so a
read is one indexed query. Per ``(host, dir)``:

* a **cursor** ``(db_id, seq)`` for a dir whose index has the change sequence —
  its records are kept current by deltas;
* a **snapshot** (``db_id`` NULL) for a dir read directly — a pre-v4 index or a
  dir with no index (log-parsed) — whose records are replaced wholesale on each
  pull, so it never needs a cursor.

Only *finished* runs are cached (History shows outcomes; the Running-now strip asks
the host live). Records older than the cache window are trimmed on every pull.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS recs (
  host TEXT NOT NULL, dir TEXT NOT NULL, key TEXT NOT NULL,
  ts TEXT NOT NULL, rec TEXT NOT NULL,
  PRIMARY KEY (host, dir, key)
);
CREATE INDEX IF NOT EXISTS recs_host_ts ON recs (host, ts);
CREATE TABLE IF NOT EXISTS cursors (
  host TEXT NOT NULL, dir TEXT NOT NULL, db_id TEXT, seq INTEGER,
  PRIMARY KEY (host, dir)
);
CREATE TABLE IF NOT EXISTS hosts (
  host TEXT PRIMARY KEY, synced_at TEXT, error TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HistoryCache:
    """Thread-safe (one connection behind a lock). Every method is best-effort:
    a SQLite failure drops and rebuilds the file once, then gives up quietly — a
    broken cache only ever means "read the host directly", never a failed read."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._con: sqlite3.Connection | None = None

    # ── connection ────────────────────────────────────────────────────────────

    def _open(self) -> sqlite3.Connection:
        if self._con is not None:
            return self._con
        try:
            self._con = self._connect()
        except sqlite3.DatabaseError:
            self._reset_file()
            self._con = self._connect()
        return self._con

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        try:
            con.executescript(_DDL)
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            con.close()  # a corrupt file: don't leak the handle before it's rebuilt
            raise
        return con

    def _reset_file(self) -> None:
        if self._con is not None:
            with contextlib.suppress(sqlite3.Error):
                self._con.close()
            self._con = None
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                Path(f"{self.path}{suffix}").unlink()

    def _run(self, fn: Any, default: Any) -> Any:
        """Run ``fn(con)`` under the lock, returning ``default`` on failure. A corrupt
        file (``DatabaseError`` — "file is not a database", a malformed page) is
        deleted and rebuilt, then the call retried once; a transient
        ``OperationalError`` (locked, disk full) just returns ``default`` — it never
        throws the cache away."""
        with self._lock:
            for attempt in (0, 1):
                try:
                    con = self._open()
                    out = fn(con)
                    con.commit()
                    return out
                except sqlite3.OperationalError:
                    with contextlib.suppress(sqlite3.Error):
                        if self._con is not None:
                            self._con.rollback()
                    return default
                except sqlite3.Error:
                    self._reset_file()
                    if attempt:
                        return default
            return default

    # ── cursors ───────────────────────────────────────────────────────────────

    def cursors(self, host: str) -> dict[str, dict[str, Any]]:
        """``{dir: {"db_id", "seq"}}`` for every dir of ``host`` the cache has pulled
        (``db_id`` None for a snapshot dir)."""

        def q(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            return {
                d: {"db_id": db_id, "seq": seq}
                for d, db_id, seq in con.execute(
                    "SELECT dir, db_id, seq FROM cursors WHERE host=?", (host,)
                )
            }

        out: dict[str, dict[str, Any]] = self._run(q, {})
        return out

    def is_primed(self, host: str) -> bool:
        """Whether ``host`` has been pulled successfully at least once."""
        return bool(self.status(host).get("synced_at"))

    # ── writes ────────────────────────────────────────────────────────────────

    def apply(
        self,
        host: str,
        dir_: str,
        result: dict[str, Any],
        records: list[dict[str, Any]],
        deleted_keys: list[str],
    ) -> bool:
        """Apply one ``delta``/``reset`` page for ``(host, dir_)``: ``records`` are the
        mapped finished records to upsert (keyed by ``runId``), ``deleted_keys`` the
        run ids to drop (deletes *and* rows that turned out to be running). A reset
        drops the dir's records first. Stores the page's cursor. Returns whether
        anything the tab shows may have changed."""

        def w(con: sqlite3.Connection) -> bool:
            changed = False
            if result.get("mode") == "reset":
                changed = (
                    con.execute(
                        "DELETE FROM recs WHERE host=? AND dir=?", (host, dir_)
                    ).rowcount
                    > 0
                )
            for key in deleted_keys:
                changed |= (
                    con.execute(
                        "DELETE FROM recs WHERE host=? AND dir=? AND key=?",
                        (host, dir_, key),
                    ).rowcount
                    > 0
                )
            for rec in records:
                con.execute(
                    "INSERT OR REPLACE INTO recs (host, dir, key, ts, rec) VALUES (?,?,?,?,?)",
                    (
                        host,
                        dir_,
                        str(rec.get("runId") or rec.get("id") or ""),
                        str(rec.get("ts") or ""),
                        json.dumps(rec),
                    ),
                )
                changed = True
            con.execute(
                "INSERT OR REPLACE INTO cursors (host, dir, db_id, seq) VALUES (?,?,?,?)",
                (host, dir_, result.get("db_id"), int(result.get("seq") or 0)),
            )
            return changed

        out: bool = self._run(w, False)
        return out

    def replace(self, host: str, dir_: str, records: list[dict[str, Any]]) -> bool:
        """Replace a snapshot dir's records wholesale (a pre-v4 index read directly,
        or a log-parsed dir). Returns whether the set of shown records changed."""

        def w(con: sqlite3.Connection) -> bool:
            before = {
                (k, r)
                for k, r in con.execute(
                    "SELECT key, rec FROM recs WHERE host=? AND dir=?", (host, dir_)
                )
            }
            con.execute("DELETE FROM recs WHERE host=? AND dir=?", (host, dir_))
            after = set()
            for rec in records:
                key = str(rec.get("runId") or rec.get("id") or "")
                blob = json.dumps(rec)
                after.add((key, blob))
                con.execute(
                    "INSERT OR REPLACE INTO recs (host, dir, key, ts, rec) VALUES (?,?,?,?,?)",
                    (host, dir_, key, str(rec.get("ts") or ""), blob),
                )
            con.execute(
                "INSERT OR REPLACE INTO cursors (host, dir, db_id, seq) VALUES (?,?,NULL,0)",
                (host, dir_),
            )
            return before != after

        out: bool = self._run(w, False)
        return out

    def drop_dirs(self, host: str, keep: set[str]) -> bool:
        """Forget ``host``'s dirs that aren't in ``keep`` (a venv removed from the host
        entry). Returns whether any records went."""

        def w(con: sqlite3.Connection) -> bool:
            gone = [
                d
                for (d,) in con.execute("SELECT dir FROM cursors WHERE host=?", (host,))
                if d not in keep
            ]
            n = 0
            for d in gone:
                n += con.execute(
                    "DELETE FROM recs WHERE host=? AND dir=?", (host, d)
                ).rowcount
                con.execute("DELETE FROM cursors WHERE host=? AND dir=?", (host, d))
            return n > 0

        out: bool = self._run(w, False)
        return out

    def trim(self, before_ts: str) -> None:
        """Drop records older than ``before_ts`` (the cache window)."""
        self._run(
            lambda con: con.execute("DELETE FROM recs WHERE ts < ?", (before_ts,)), None
        )

    def retain_hosts(self, hosts: set[str]) -> None:
        """Forget every host not in ``hosts`` (removed or renamed in the host list)."""

        def w(con: sqlite3.Connection) -> None:
            known = {h for (h,) in con.execute("SELECT host FROM hosts")} | {
                h for (h,) in con.execute("SELECT DISTINCT host FROM cursors")
            }
            for h in known - hosts:
                for table in ("recs", "cursors", "hosts"):
                    con.execute(f"DELETE FROM {table} WHERE host=?", (h,))

        self._run(w, None)

    def mark(self, host: str, *, error: str = "") -> None:
        """Record a pull's outcome: success stamps ``synced_at``; a failure keeps the
        last good ``synced_at`` and records ``error`` (so the tab can say "as of …")."""

        def w(con: sqlite3.Connection) -> None:
            if error:
                con.execute(
                    "INSERT INTO hosts (host, synced_at, error) VALUES (?, NULL, ?) ON CONFLICT(host) DO UPDATE SET error=excluded.error",
                    (host, error),
                )
            else:
                con.execute(
                    "INSERT OR REPLACE INTO hosts (host, synced_at, error) VALUES (?, ?, '')",
                    (host, _now_iso()),
                )

        self._run(w, None)

    # ── reads ─────────────────────────────────────────────────────────────────

    def read(
        self, host: str, *, since_date: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """``host``'s cached records, newest first, ``ts`` on/after ``since_date``
        (``YYYY-MM-DD``), at most ``limit``."""

        def q(con: sqlite3.Connection) -> list[dict[str, Any]]:
            sql, params = "SELECT rec FROM recs WHERE host=?", [host]
            if since_date:
                sql += " AND ts >= ?"
                params.append(since_date)
            sql += " ORDER BY ts DESC LIMIT ?"
            params.append(max(1, int(limit)))
            out = []
            for (blob,) in con.execute(sql, params):
                with contextlib.suppress(ValueError, TypeError):
                    out.append(json.loads(blob))
            return out

        rows: list[dict[str, Any]] = self._run(q, [])
        return rows

    def status(self, host: str) -> dict[str, Any]:
        """``{"synced_at", "error"}`` for ``host`` (empty values when never pulled)."""

        def q(con: sqlite3.Connection) -> dict[str, Any]:
            r = con.execute(
                "SELECT synced_at, error FROM hosts WHERE host=?", (host,)
            ).fetchone()
            return {
                "synced_at": r[0] if r else None,
                "error": (r[1] or "") if r else "",
            }

        out: dict[str, Any] = self._run(q, {"synced_at": None, "error": ""})
        return out

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._con.close()
                self._con = None

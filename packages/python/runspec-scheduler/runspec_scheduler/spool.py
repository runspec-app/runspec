"""spool.py — the webhook spool: ``{venv}/hooks/spool.sqlite``.

Every accepted request is one row before the sender gets its ``202``, so
nothing is lost when a run is slow, the daemon restarts or no console is
watching. Consoles read the ``event`` table as a stream (``hooks follow
--since <id>``); approvals, denials and mutes are written here too, so the
first click wins on the host and every console sees who did what.

Tables:

* ``delivery`` — one per received item (a split body gives several): the body
  as sent, fields, the rule, what it runs, its state and who decided.
* ``event`` — append-only: each new delivery and each change, with a snapshot
  of the delivery (and the mute changes). ``id`` is the follow cursor.
* ``mute`` — "Ann muted jira-deploy until 17:00".
* ``follower`` — consoles following right now (for the email fallback).

Owner-only (the dir is 0700, the file 0600). WAL, so the listener, the worker
and the CLI verbs can write at once. Stdlib only.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

SPOOL_DIR = "hooks"
SPOOL_NAME = "spool.sqlite"

#: Delivery states. ``queued`` → worked out by the daemon → one of the rest.
QUEUED = "queued"
PENDING = "pending"  # waiting for an approval
APPROVED = "approved"  # approved, waiting to start
WAITING = "waiting"  # autonomous, waiting for a run slot
RUNNING = "running"
RAN = "ran"  # finished (see exit_code)
DENIED = "denied"
EXPIRED = "expired"
REFUSED = "refused"  # manual runnable, denylist, can't plan
SKIPPED = "skipped"  # a rule said skip, or skip_if_running
EVENT = "event"  # matched a rule with no run: consoles only
NO_MATCH = "no-match"
DUPLICATE = "duplicate"
ERROR = "error"  # couldn't read the body / work out the run

SETTLED = {RAN, DENIED, EXPIRED, REFUSED, SKIPPED, EVENT, NO_MATCH, DUPLICATE, ERROR}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hook TEXT NOT NULL,
    received_ts REAL NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    headers TEXT NOT NULL DEFAULT '{}',
    body BLOB,
    size INTEGER NOT NULL DEFAULT 0,
    request INTEGER,
    item INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    fields TEXT NOT NULL DEFAULT '{}',
    rule TEXT NOT NULL DEFAULT '',
    runnable TEXT NOT NULL DEFAULT '',
    command TEXT NOT NULL DEFAULT '[]',
    args TEXT NOT NULL DEFAULT '{}',
    autonomy TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    dedup_key TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 1,
    dup_of INTEGER,
    expires_ts REAL,
    decided_by TEXT NOT NULL DEFAULT '',
    decided_ts REAL,
    note TEXT NOT NULL DEFAULT '',
    started_ts REAL,
    finished_ts REAL,
    exit_code INTEGER,
    pid INTEGER,
    emailed_ts REAL,
    mail TEXT NOT NULL DEFAULT '',
    updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS delivery_state ON delivery(state);
CREATE INDEX IF NOT EXISTS delivery_hook ON delivery(hook, received_ts);
CREATE INDEX IF NOT EXISTS delivery_dedup ON delivery(hook, dedup_key, received_ts);
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    delivery INTEGER,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_delivery ON event(delivery);
CREATE TABLE IF NOT EXISTS mute (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    who TEXT NOT NULL,
    hook TEXT NOT NULL,
    until_ts REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS follower (
    token TEXT PRIMARY KEY,
    who TEXT NOT NULL,
    pid INTEGER,
    since_ts REAL NOT NULL,
    seen_ts REAL NOT NULL
);
"""

#: A follower not seen for this long isn't following.
FOLLOWER_STALE_SECONDS = 30.0
#: Body text in an event is cut to this (the full body stays in ``delivery``).
EVENT_BODY_BYTES = 64 * 1024


def spool_path(venv: Path | None = None) -> Path:
    return (venv or Path(sys.prefix)) / SPOOL_DIR / SPOOL_NAME


def spool_path_for(config_path: Path) -> Path:
    """The spool next to the config (``{venv}/hooks/spool.sqlite`` by default)."""
    return config_path.parent / SPOOL_DIR / SPOOL_NAME


class Spool:
    """One connection per thread to the spool file (sqlite3 connections aren't
    shared across threads)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or spool_path()
        self._local = threading.local()
        self._ensure()

    def _ensure(self) -> None:
        folder = self.path.parent
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(folder, 0o700)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        conn = self.conn()
        conn.executescript(_SCHEMA)

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ── deliveries ──

    def add_delivery(
        self,
        hook: str,
        *,
        source: str,
        content_type: str,
        headers: dict[str, str],
        body: bytes,
        state: str = QUEUED,
        request: int | None = None,
        item: int = 0,
        fields: dict[str, Any] | None = None,
        received_ts: float | None = None,
    ) -> int:
        now = time.time()
        conn = self.conn()
        cur = conn.execute(
            "INSERT INTO delivery (hook, received_ts, source, content_type, headers, body, size, request, item, state, fields, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (hook, received_ts or now, source, content_type, json.dumps(headers), body, len(body), request, item, state, json.dumps(fields or {}, default=str), now),
        )
        return int(cur.lastrowid or 0)

    def get(self, delivery_id: int) -> dict[str, Any] | None:
        row = self.conn().execute("SELECT * FROM delivery WHERE id = ?", (delivery_id,)).fetchone()
        return _row(row) if row else None

    def queued(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn().execute("SELECT * FROM delivery WHERE state = ? ORDER BY id LIMIT ?", (QUEUED, limit)).fetchall()
        return [_row(r) for r in rows]

    def in_state(self, *states: str, hook: str | None = None) -> list[dict[str, Any]]:
        q = f"SELECT * FROM delivery WHERE state IN ({','.join('?' * len(states))})"
        params: list[Any] = list(states)
        if hook:
            q += " AND hook = ?"
            params.append(hook)
        rows = self.conn().execute(q + " ORDER BY id", params).fetchall()
        return [_row(r) for r in rows]

    def count(self, *states: str) -> int:
        q = f"SELECT COUNT(*) FROM delivery WHERE state IN ({','.join('?' * len(states))})"
        return int(self.conn().execute(q, list(states)).fetchone()[0])

    def recent(self, *, hook: str | None = None, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q = "SELECT * FROM delivery WHERE 1=1"
        params: list[Any] = []
        if hook:
            q += " AND hook = ?"
            params.append(hook)
        if state:
            q += " AND state = ?"
            params.append(state)
        rows = self.conn().execute(q + " ORDER BY id DESC LIMIT ?", [*params, limit]).fetchall()
        return [_row(r) for r in rows]

    def last_body(self, hook: str) -> dict[str, Any] | None:
        """The newest delivery of ``hook`` that still has its body (for ``hooks test``)."""
        row = self.conn().execute("SELECT * FROM delivery WHERE hook = ? AND body IS NOT NULL AND item = 0 ORDER BY id DESC LIMIT 1", (hook,)).fetchone()
        return _row(row) if row else None

    def update(self, delivery_id: int, *, kind: str = "state", when: str | None = None, with_body: bool = False, **changes: Any) -> bool:
        """Change a delivery and record the change as an event. With ``when``
        (a SQL condition on the current row, e.g. ``state = 'pending'``) the
        change only happens if it still holds — the first approval wins."""
        if not changes:
            return False
        changes["updated_ts"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in changes)
        values = [json.dumps(v, default=str) if k in ("fields", "command", "args") else v for k, v in changes.items()]
        conn = self.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            q = f"UPDATE delivery SET {sets} WHERE id = ?"
            params = [*values, delivery_id]
            if when:
                q += f" AND ({when})"
            cur = conn.execute(q, params)
            changed = cur.rowcount > 0
            if changed:
                row = conn.execute("SELECT * FROM delivery WHERE id = ?", (delivery_id,)).fetchone()
                self._event(conn, kind, delivery_id, snapshot(_row(row), body=with_body))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return changed

    def announce(self, delivery_id: int, kind: str = "delivery", *, body: bool = True) -> int:
        """Record a delivery's current state as an event (the first one carries
        the body text, cut to :data:`EVENT_BODY_BYTES`)."""
        row = self.get(delivery_id)
        if row is None:
            return 0
        data = snapshot(row, body=body)
        conn = self.conn()
        return self._event(conn, kind, delivery_id, data)

    def bump(self, delivery_id: int) -> None:
        """Another copy of a delivery arrived (same dedup key while it waits)."""
        conn = self.conn()
        conn.execute("UPDATE delivery SET count = count + 1, updated_ts = ? WHERE id = ?", (time.time(), delivery_id))
        row = conn.execute("SELECT * FROM delivery WHERE id = ?", (delivery_id,)).fetchone()
        if row:
            self._event(conn, "repeat", delivery_id, snapshot(_row(row)))

    def find_dedup(self, hook: str, key: str, since_ts: float, exclude: int) -> dict[str, Any] | None:
        row = (
            self.conn()
            .execute(
                "SELECT * FROM delivery WHERE hook = ? AND dedup_key = ? AND received_ts >= ? AND id != ? AND dup_of IS NULL AND state NOT IN (?, ?, ?) ORDER BY id DESC LIMIT 1",
                (hook, key, since_ts, exclude, QUEUED, ERROR, NO_MATCH),
            )
            .fetchone()
        )
        return _row(row) if row else None

    # ── events ──

    def _event(self, conn: sqlite3.Connection, kind: str, delivery_id: int | None, data: dict[str, Any]) -> int:
        cur = conn.execute("INSERT INTO event (ts, kind, delivery, data) VALUES (?,?,?,?)", (time.time(), kind, delivery_id, json.dumps(data, default=str)))
        return int(cur.lastrowid or 0)

    def add_event(self, kind: str, data: dict[str, Any], delivery_id: int | None = None) -> int:
        return self._event(self.conn(), kind, delivery_id, data)

    def events_since(self, since: int, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.conn().execute("SELECT * FROM event WHERE id > ? ORDER BY id LIMIT ?", (since, limit)).fetchall()
        out = []
        for r in rows:
            data = json.loads(r["data"])
            out.append({"event": int(r["id"]), "ts": _iso(r["ts"]), "kind": r["kind"], **data})
        return out

    def last_event_id(self) -> int:
        row = self.conn().execute("SELECT MAX(id) FROM event").fetchone()
        return int(row[0] or 0)

    def first_event_after(self, ts: float) -> int:
        """The cursor just before the first event at/after ``ts`` (a replay window)."""
        row = self.conn().execute("SELECT MIN(id) FROM event WHERE ts >= ?", (ts,)).fetchone()
        return int(row[0]) - 1 if row and row[0] else self.last_event_id()

    # ── mutes ──

    def mute(self, who: str, hook: str, until_ts: float, reason: str = "") -> dict[str, Any]:
        conn = self.conn()
        conn.execute("DELETE FROM mute WHERE who = ? AND hook = ?", (who, hook))
        conn.execute("INSERT INTO mute (who, hook, until_ts, reason, created_ts) VALUES (?,?,?,?,?)", (who, hook, until_ts, reason, time.time()))
        data = {"who": who, "hook": hook, "until": _iso(until_ts), "reason": reason}
        self._event(conn, "mute", None, data)
        return data

    def unmute(self, who: str, hook: str) -> bool:
        conn = self.conn()
        cur = conn.execute("DELETE FROM mute WHERE who = ? AND (hook = ? OR ? = '*')", (who, hook, hook))
        if cur.rowcount:
            self._event(conn, "unmute", None, {"who": who, "hook": hook})
        return cur.rowcount > 0

    def mutes(self, now: float | None = None) -> list[dict[str, Any]]:
        now = now or time.time()
        rows = self.conn().execute("SELECT * FROM mute WHERE until_ts > ? ORDER BY until_ts", (now,)).fetchall()
        return [{"who": r["who"], "hook": r["hook"], "until": _iso(r["until_ts"]), "reason": r["reason"]} for r in rows]

    def is_muted(self, who: str, hook: str, now: float | None = None) -> bool:
        now = now or time.time()
        row = self.conn().execute("SELECT 1 FROM mute WHERE who = ? AND (hook = ? OR hook = '*') AND until_ts > ? LIMIT 1", (who, hook, now)).fetchone()
        return row is not None

    # ── followers ──

    def follower_seen(self, token: str, who: str, pid: int) -> None:
        now = time.time()
        self.conn().execute(
            "INSERT INTO follower (token, who, pid, since_ts, seen_ts) VALUES (?,?,?,?,?) ON CONFLICT(token) DO UPDATE SET seen_ts = excluded.seen_ts",
            (token, who, pid, now, now),
        )

    def follower_gone(self, token: str) -> None:
        self.conn().execute("DELETE FROM follower WHERE token = ?", (token,))

    def followers(self, now: float | None = None) -> list[dict[str, Any]]:
        now = now or time.time()
        rows = self.conn().execute("SELECT * FROM follower WHERE seen_ts > ?", (now - FOLLOWER_STALE_SECONDS,)).fetchall()
        return [{"who": r["who"], "pid": r["pid"], "since": _iso(r["since_ts"])} for r in rows]

    def watching(self, hook: str, now: float | None = None) -> list[str]:
        """Who follows right now and hasn't muted ``hook``."""
        now = now or time.time()
        return sorted({f["who"] for f in self.followers(now) if not self.is_muted(f["who"], hook, now)})

    # ── upkeep ──

    def prune(self, keep_seconds: int, now: float | None = None) -> int:
        """Drop settled deliveries (and their events) older than ``keep_seconds``,
        old followers and expired mutes. Returns the deliveries dropped."""
        now = now or time.time()
        cutoff = now - keep_seconds
        conn = self.conn()
        settled = tuple(SETTLED)
        marks = ",".join("?" * len(settled))
        cur = conn.execute(f"DELETE FROM delivery WHERE received_ts < ? AND state IN ({marks})", (cutoff, *settled))
        conn.execute("DELETE FROM event WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM mute WHERE until_ts < ?", (now,))
        conn.execute("DELETE FROM follower WHERE seen_ts < ?", (now - 3600,))
        return cur.rowcount


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    import datetime

    return datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc).isoformat()


def _row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in ("headers", "fields", "args", "command"):
        try:
            out[key] = json.loads(out.get(key) or ("[]" if key == "command" else "{}"))
        except ValueError:
            out[key] = {}
    return out


def body_text(row: dict[str, Any], limit: int | None = EVENT_BODY_BYTES) -> str:
    raw = row.get("body") or b""
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, str):
        raw = raw.encode()
    cut = raw if limit is None else raw[:limit]
    return cut.decode("utf-8", errors="replace")


def snapshot(row: dict[str, Any], *, body: bool = False) -> dict[str, Any]:
    """A delivery as followers see it (times as ISO, the body only on request)."""
    out = {
        "id": row["id"],
        "hook": row["hook"],
        "received": _iso(row["received_ts"]),
        "source": row["source"],
        "state": row["state"],
        "fields": row["fields"],
        "rule": row["rule"],
        "runnable": row["runnable"],
        "command": row["command"],
        "args": row["args"],
        "autonomy": row["autonomy"],
        "reason": row["reason"],
        "count": row["count"],
        "dup_of": row["dup_of"],
        "expires": _iso(row["expires_ts"]),
        "decided_by": row["decided_by"],
        "decided": _iso(row["decided_ts"]),
        "note": row["note"],
        "started": _iso(row["started_ts"]),
        "finished": _iso(row["finished_ts"]),
        "exit_code": row["exit_code"],
        "emailed": _iso(row["emailed_ts"]),
        "mail": row.get("mail", ""),
        "item": row["item"],
        "request": row["request"],
        "content_type": row["content_type"],
    }
    if body:
        out["body"] = body_text(row)
        out["body_truncated"] = (row.get("size") or 0) > EVENT_BODY_BYTES
    return out

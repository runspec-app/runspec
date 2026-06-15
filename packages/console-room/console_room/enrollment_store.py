"""
enrollment_store.py — durable storage for email-verified queue signup (SQLite).

Two tables behind the email-verify auth mode (see
docs/design/email-queue-signup.md §6):

- ``members`` — verified enrollments ``(email, queue, name, verified_at)``: a
  returning user keyed by ``(email, queue)``.
- ``pending_signups`` — the durable verify outbox + single-use nonce
  ``(nonce, email, name, queue, created_at, send_state, claimed_by)``: a signup
  awaiting its verify email. ``send_state`` walks ``pending → claimed → sent``;
  ``nonce`` is burned on verify to make the link single-use.

Same shape as :class:`~console_room.store.TranscriptStore`: one connection,
``check_same_thread=False`` + a lock, synchronous methods the hub drives off the
event loop via ``asyncio.to_thread``. Stdlib only. The pure token/domain helpers
live in ``signup.py``; this module is just the I/O.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .signup import Member, PendingSignup, Team

#: The always-present fallback team — seeded so the sign-up team list is never
#: empty, and protected from removal so it stays the safety net.
DEFAULT_TEAM = Team(slug="default", name="Default")


class EnrollmentStore:
    """SQLite-backed enrollment + verify-outbox store. One shared connection,
    guarded by a lock so ``to_thread`` workers can share it safely."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        path = str(db_path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS members (
                    email       TEXT NOT NULL,
                    queue       TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    verified_at REAL NOT NULL,
                    PRIMARY KEY (email, queue)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_signups (
                    nonce      TEXT PRIMARY KEY,
                    email      TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    queue      TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    send_state TEXT NOT NULL DEFAULT 'pending',
                    claimed_by TEXT NOT NULL DEFAULT ''
                )
                """
            )
            # The outbox flush queries by (queue, send_state) — index it.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pending_queue_state "
                "ON pending_signups (queue, send_state)"
            )
            # Customer teams (departments) a visitor picks at sign-up — slug + name,
            # ordered by creation so the seeded Default sorts first.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS teams (
                    slug       TEXT PRIMARY KEY,
                    name       TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            # Seed Default once, so the team list is never empty.
            self._conn.execute(
                "INSERT OR IGNORE INTO teams (slug, name, created_at) VALUES (?, ?, ?)",
                (DEFAULT_TEAM.slug, DEFAULT_TEAM.name, time.time()),
            )
            self._conn.commit()

    # ── teams (customer departments; the sign-up dropdown) ─────────────────────

    def list_teams(self) -> list[Team]:
        """Every team, the seeded Default first (oldest), then by creation order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT slug, name FROM teams ORDER BY created_at, slug"
            ).fetchall()
        return [Team(slug=r[0], name=r[1]) for r in rows]

    def get_team(self, slug: str) -> Team | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT slug, name FROM teams WHERE slug = ?", (slug,)
            ).fetchone()
        return Team(slug=row[0], name=row[1]) if row else None

    def add_team(self, team: Team) -> None:
        """Create or rename a team (upsert by slug)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO teams (slug, name, created_at) VALUES (?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET name = excluded.name
                """,
                (team.slug, team.name, time.time()),
            )
            self._conn.commit()

    def remove_team(self, slug: str) -> bool:
        """Delete a team. Refuses to remove the protected ``Default`` (so the list
        is never empty). Returns True when a row was deleted."""
        if slug == DEFAULT_TEAM.slug:
            return False
        with self._lock:
            deleted = self._conn.execute(
                "DELETE FROM teams WHERE slug = ?", (slug,)
            ).rowcount
            self._conn.commit()
        return deleted > 0

    # ── pending signups (the verify outbox) ───────────────────────────────────

    def add_pending(self, signup: PendingSignup) -> None:
        """Persist a new pending signup (state ``pending`` by default). The
        ``nonce`` is the single-use key — a duplicate is ignored, not raised."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO pending_signups "
                "(nonce, email, name, queue, created_at, send_state, claimed_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    signup.nonce,
                    signup.email,
                    signup.name,
                    signup.queue,
                    signup.created_at,
                    signup.send_state,
                    signup.claimed_by,
                ),
            )
            self._conn.commit()

    def get_pending(self, nonce: str) -> PendingSignup | None:
        """The pending signup for ``nonce``, or ``None`` if it's gone (consumed,
        swept, or never existed)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT nonce, email, name, queue, created_at, send_state, claimed_by "
                "FROM pending_signups WHERE nonce = ?",
                (nonce,),
            ).fetchone()
        return _pending_from_row(row) if row else None

    def consume_pending(self, nonce: str) -> PendingSignup | None:
        """Burn a pending signup: delete its row and return it, or ``None`` if it
        was already gone. The delete is atomic, so a token can verify only once
        even if two clicks race — exactly one caller gets the row back."""
        with self._lock:
            row = self._conn.execute(
                "SELECT nonce, email, name, queue, created_at, send_state, claimed_by "
                "FROM pending_signups WHERE nonce = ?",
                (nonce,),
            ).fetchone()
            if row is None:
                return None
            deleted = self._conn.execute(
                "DELETE FROM pending_signups WHERE nonce = ?", (nonce,)
            ).rowcount
            self._conn.commit()
        # rowcount 0 means another thread consumed it between our read and delete.
        return _pending_from_row(row) if deleted else None

    def claim_pending_sends(self, queue: str, console: str) -> list[PendingSignup]:
        """Claim every ``pending`` send for ``queue`` for ``console`` — the
        flush-on-connect step. Atomically flips each ``pending`` row to
        ``claimed`` stamped with ``console``, so two consoles connecting together
        never both send. Returns the rows this call claimed (already in
        ``claimed`` state)."""
        with self._lock:
            self._conn.execute(
                "UPDATE pending_signups SET send_state = 'claimed', claimed_by = ? "
                "WHERE queue = ? AND send_state = 'pending'",
                (console, queue),
            )
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT nonce, email, name, queue, created_at, send_state, claimed_by "
                "FROM pending_signups "
                "WHERE queue = ? AND send_state = 'claimed' AND claimed_by = ?",
                (queue, console),
            ).fetchall()
        return [_pending_from_row(r) for r in rows]

    def mark_sent(self, nonce: str) -> None:
        """Mark a claimed send as ``sent`` (the console acked success)."""
        with self._lock:
            self._conn.execute(
                "UPDATE pending_signups SET send_state = 'sent' WHERE nonce = ?",
                (nonce,),
            )
            self._conn.commit()

    def release_claim(self, nonce: str) -> None:
        """Return a claimed send to ``pending`` (the console failed / disconnected)
        so another console can pick it up on its next flush."""
        with self._lock:
            self._conn.execute(
                "UPDATE pending_signups SET send_state = 'pending', claimed_by = '' "
                "WHERE nonce = ?",
                (nonce,),
            )
            self._conn.commit()

    def release_console_claims(self, console: str) -> list[str]:
        """Return every send ``console`` claimed-but-never-acked to ``pending`` (its
        link dropped before it could send / ack), so another console can pick them
        up. Returns the distinct queues affected (the caller re-flushes them)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT queue FROM pending_signups "
                "WHERE send_state = 'claimed' AND claimed_by = ?",
                (console,),
            ).fetchall()
            self._conn.execute(
                "UPDATE pending_signups SET send_state = 'pending', claimed_by = '' "
                "WHERE send_state = 'claimed' AND claimed_by = ?",
                (console,),
            )
            self._conn.commit()
        return [r[0] for r in rows]

    def sweep_expired(self, max_age: float, now: float | None = None) -> int:
        """Drop pending signups older than ``max_age`` seconds (their verify link
        has expired; the user re-signs). Returns the number removed. ``sent`` rows
        are kept — they're the audit trail of issued verifies."""
        cutoff = (time.time() if now is None else now) - max_age
        with self._lock:
            removed = self._conn.execute(
                "DELETE FROM pending_signups "
                "WHERE created_at < ? AND send_state != 'sent'",
                (cutoff,),
            ).rowcount
            self._conn.commit()
        return removed

    # ── members (verified enrollments) ────────────────────────────────────────

    def add_member(self, member: Member) -> None:
        """Persist (or refresh) a verified enrollment. Re-verifying updates the
        name + timestamp for the same ``(email, queue)``."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO members (email, queue, name, verified_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (email, queue) DO UPDATE SET "
                "name = excluded.name, verified_at = excluded.verified_at",
                (member.email, member.queue, member.name, member.verified_at),
            )
            self._conn.commit()

    def get_member(self, email: str, queue: str) -> Member | None:
        """The verified member for ``(email, queue)``, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT email, queue, name, verified_at FROM members "
                "WHERE email = ? AND queue = ?",
                (email, queue),
            ).fetchone()
        if row is None:
            return None
        return Member(email=row[0], queue=row[1], name=row[2], verified_at=row[3])

    def is_member(self, email: str, queue: str) -> bool:
        """Whether ``email`` is a verified member of ``queue``."""
        return self.get_member(email, queue) is not None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _pending_from_row(row: tuple) -> PendingSignup:
    return PendingSignup(
        nonce=row[0],
        email=row[1],
        name=row[2],
        queue=row[3],
        created_at=row[4],
        send_state=row[5],
        claimed_by=row[6],
    )

"""
test_enrollment_store.py — the SQLite enrollment + verify-outbox store:
pending lifecycle (add/get/consume burn), the flush-on-connect claim, send
acks/release, expiry sweep, member upsert, and on-disk durability.
"""

from __future__ import annotations

from console_room.enrollment_store import EnrollmentStore
from console_room.signup import Member, PendingSignup, Team


def _pending(nonce="n1", email="jane@corp.com", queue="it-support", created_at=100.0):
    return PendingSignup(
        nonce=nonce, email=email, name="Jane Doe", queue=queue, created_at=created_at
    )


# ── pending signups ───────────────────────────────────────────────────────────


def test_add_and_get_pending():
    s = EnrollmentStore(":memory:")
    s.add_pending(_pending())
    got = s.get_pending("n1")
    assert got is not None
    assert got.email == "jane@corp.com"
    assert got.name == "Jane Doe"
    assert got.send_state == "pending"
    assert got.claimed_by == ""


def test_get_pending_missing_is_none():
    s = EnrollmentStore(":memory:")
    assert s.get_pending("nope") is None


def test_add_pending_idempotent_on_nonce():
    s = EnrollmentStore(":memory:")
    s.add_pending(_pending(email="first@corp.com"))
    s.add_pending(_pending(email="second@corp.com"))  # same nonce → ignored
    got = s.get_pending("n1")
    assert got is not None and got.email == "first@corp.com"


def test_consume_pending_returns_and_burns():
    s = EnrollmentStore(":memory:")
    s.add_pending(_pending())
    got = s.consume_pending("n1")
    assert got is not None and got.email == "jane@corp.com"
    # Burned — a second consume gets nothing (single-use link).
    assert s.consume_pending("n1") is None
    assert s.get_pending("n1") is None


def test_consume_missing_is_none():
    s = EnrollmentStore(":memory:")
    assert s.consume_pending("nope") is None


# ── flush-on-connect (claim) ──────────────────────────────────────────────────


def test_claim_pending_sends_flips_and_returns():
    s = EnrollmentStore(":memory:")
    s.add_pending(_pending(nonce="a", queue="it-support"))
    s.add_pending(_pending(nonce="b", queue="it-support"))
    s.add_pending(_pending(nonce="c", queue="other"))  # different queue
    claimed = s.claim_pending_sends("it-support", "console-1")
    nonces = sorted(p.nonce for p in claimed)
    assert nonces == ["a", "b"]
    assert all(
        p.send_state == "claimed" and p.claimed_by == "console-1" for p in claimed
    )
    # The other-queue row is untouched.
    other = s.get_pending("c")
    assert other is not None and other.send_state == "pending"


def test_claim_is_exclusive_between_consoles():
    s = EnrollmentStore(":memory:")
    s.add_pending(_pending(nonce="a", queue="it-support"))
    first = s.claim_pending_sends("it-support", "console-1")
    second = s.claim_pending_sends("it-support", "console-2")
    # First console claims it; the second sees nothing left to claim.
    assert [p.nonce for p in first] == ["a"]
    assert second == []


def test_claim_returns_only_this_consoles_rows():
    s = EnrollmentStore(":memory:")
    s.add_pending(_pending(nonce="a", queue="it-support"))
    s.claim_pending_sends("it-support", "console-1")
    # A new pending arrives, claimed by a different console.
    s.add_pending(_pending(nonce="b", queue="it-support"))
    claimed = s.claim_pending_sends("it-support", "console-2")
    assert [p.nonce for p in claimed] == ["b"]


def test_mark_sent_and_not_reclaimed():
    s = EnrollmentStore(":memory:")
    s.add_pending(_pending(nonce="a", queue="it-support"))
    s.claim_pending_sends("it-support", "console-1")
    s.mark_sent("a")
    assert s.get_pending("a").send_state == "sent"
    # A later flush doesn't resend an already-sent row.
    assert s.claim_pending_sends("it-support", "console-2") == []


def test_release_claim_returns_to_pending():
    s = EnrollmentStore(":memory:")
    s.add_pending(_pending(nonce="a", queue="it-support"))
    s.claim_pending_sends("it-support", "console-1")
    s.release_claim("a")
    got = s.get_pending("a")
    assert got.send_state == "pending" and got.claimed_by == ""
    # Now another console can pick it up.
    claimed = s.claim_pending_sends("it-support", "console-2")
    assert [p.nonce for p in claimed] == ["a"]


# ── expiry sweep ──────────────────────────────────────────────────────────────


def test_sweep_expired_drops_old_pending():
    s = EnrollmentStore(":memory:")
    s.add_pending(_pending(nonce="old", created_at=0.0))
    s.add_pending(_pending(nonce="new", created_at=1000.0))
    removed = s.sweep_expired(max_age=100.0, now=1000.0)
    assert removed == 1
    assert s.get_pending("old") is None
    assert s.get_pending("new") is not None


def test_sweep_keeps_sent_rows():
    s = EnrollmentStore(":memory:")
    s.add_pending(_pending(nonce="sent", created_at=0.0))
    s.claim_pending_sends("it-support", "console-1")
    s.mark_sent("sent")
    removed = s.sweep_expired(max_age=100.0, now=10_000.0)
    assert removed == 0
    assert s.get_pending("sent") is not None  # audit trail kept


# ── members ───────────────────────────────────────────────────────────────────


def test_add_get_and_is_member():
    s = EnrollmentStore(":memory:")
    assert s.is_member("jane@corp.com", "it-support") is False
    s.add_member(Member("jane@corp.com", "it-support", "Jane Doe", 5.0))
    assert s.is_member("jane@corp.com", "it-support") is True
    got = s.get_member("jane@corp.com", "it-support")
    assert got is not None and got.name == "Jane Doe" and got.verified_at == 5.0


def test_member_scoped_to_queue():
    s = EnrollmentStore(":memory:")
    s.add_member(Member("jane@corp.com", "it-support", "Jane", 1.0))
    assert s.is_member("jane@corp.com", "other") is False


def test_add_member_upserts():
    s = EnrollmentStore(":memory:")
    s.add_member(Member("jane@corp.com", "it-support", "Jane", 1.0))
    s.add_member(Member("jane@corp.com", "it-support", "Jane Smith", 2.0))
    got = s.get_member("jane@corp.com", "it-support")
    assert got.name == "Jane Smith" and got.verified_at == 2.0


def test_durable_across_reopen(tmp_path):
    db = tmp_path / "enroll.db"
    s = EnrollmentStore(db)
    s.add_member(Member("jane@corp.com", "it-support", "Jane", 1.0))
    s.add_pending(_pending(nonce="p1"))
    s.close()
    s2 = EnrollmentStore(db)
    assert s2.is_member("jane@corp.com", "it-support") is True
    assert s2.get_pending("p1") is not None
    s2.close()


# ── teams (customer departments) ──────────────────────────────────────────────


def test_default_team_seeded():
    s = EnrollmentStore(":memory:")
    teams = s.list_teams()
    assert [t.slug for t in teams] == ["default"]
    assert teams[0].name == "Default"


def test_add_list_and_order():
    s = EnrollmentStore(":memory:")
    s.add_team(Team(slug="team-one", name="Team One"))
    s.add_team(Team(slug="team-two", name="Team Two"))
    # Default (seeded first) sorts first, then by creation order.
    assert [t.slug for t in s.list_teams()] == ["default", "team-one", "team-two"]


def test_add_team_upsert_renames():
    s = EnrollmentStore(":memory:")
    s.add_team(Team(slug="hr", name="HR"))
    s.add_team(Team(slug="hr", name="Human Resources"))
    assert s.get_team("hr") == Team(slug="hr", name="Human Resources")
    assert [t.slug for t in s.list_teams()] == ["default", "hr"]  # no duplicate


def test_remove_team():
    s = EnrollmentStore(":memory:")
    s.add_team(Team(slug="hr", name="HR"))
    assert s.remove_team("hr") is True
    assert s.get_team("hr") is None
    assert s.remove_team("nope") is False  # nonexistent


def test_default_team_is_protected():
    s = EnrollmentStore(":memory:")
    assert s.remove_team("default") is False
    assert [t.slug for t in s.list_teams()] == ["default"]  # still there


def test_sync_teams_is_authoritative():
    s = EnrollmentStore(":memory:")
    s.add_team(Team(slug="stale", name="Stale"))  # a runtime-added team
    s.sync_teams(
        [Team(slug="eng", name="Engineering"), Team(slug="fin", name="Finance")]
    )
    # The config list wins: stale removed, config teams present, Default kept.
    assert [t.slug for t in s.list_teams()] == ["default", "eng", "fin"]


def test_sync_teams_keeps_default():
    s = EnrollmentStore(":memory:")
    s.sync_teams([Team(slug="eng", name="Engineering")])
    assert s.get_team("default") is not None


def test_sync_teams_upserts_name():
    s = EnrollmentStore(":memory:")
    s.add_team(Team(slug="eng", name="Eng"))
    s.sync_teams([Team(slug="eng", name="Engineering")])
    assert s.get_team("eng").name == "Engineering"


def test_sync_teams_empty_is_noop():
    s = EnrollmentStore(":memory:")
    s.add_team(Team(slug="keep", name="Keep"))
    s.sync_teams([])  # no config teams → leave the DB as-is
    assert [t.slug for t in s.list_teams()] == ["default", "keep"]


def test_teams_persist_on_disk(tmp_path):
    db = str(tmp_path / "enroll.db")
    s1 = EnrollmentStore(db)
    s1.add_team(Team(slug="ops", name="Ops"))
    s1.close()
    s2 = EnrollmentStore(db)
    assert [t.slug for t in s2.list_teams()] == ["default", "ops"]
    s2.close()

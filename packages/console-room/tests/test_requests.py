"""
test_requests.py — the claim-free portal request lifecycle + round-robin (pure).

No hub, no sockets: drive ``RequestRegistry`` directly and assert the state
machine and the fair-assignment rotation. Mirrors §3.5 of the portal design.
"""

from __future__ import annotations

from console_room.requests import (
    ASSIGNED,
    DONE,
    FAILED,
    IN_PROGRESS,
    UNASSIGNED,
    RequestRegistry,
)

IT = "helpdesk/it"


def _new(reg: RequestRegistry, runnable="reset-password", namespace=IT):
    return reg.create(
        room=f"{namespace}/alice",
        queue="it",
        namespace=namespace,
        visitor="alice@corp",
        runnable=runnable,
        args={"user": "alice"},
    )


# ── creation ──────────────────────────────────────────────────────────────────


def test_create_starts_unassigned():
    reg = RequestRegistry()
    req = _new(reg)
    assert req.state == UNASSIGNED and req.assignee is None
    assert reg.get(req.id) is req
    assert req.args == {"user": "alice"}


def test_visitor_name_defaults_empty_and_is_stored():
    reg = RequestRegistry()
    assert _new(reg).visitor_name == ""  # absent → blank (OIDC / dev)
    named = reg.create(
        room="helpdesk/it/alice",
        queue="it",
        namespace=IT,
        visitor="alice@corp",
        runnable="reset-password",
        args={},
        visitor_name="Alice Smith",
    )
    assert named.visitor_name == "Alice Smith"
    # …and it rides the assignment payload (last field) for the operator card.
    assert named.assignment_fields()[-1] == "Alice Smith"


# ── round-robin assignment ────────────────────────────────────────────────────


def test_round_robin_cycles_through_candidates():
    reg = RequestRegistry()
    picks = [reg.next_assignee(IT, ["a", "b", "c"]) for _ in range(7)]
    assert picks == ["a", "b", "c", "a", "b", "c", "a"]


def test_single_candidate_always_chosen():
    reg = RequestRegistry()
    assert [reg.next_assignee(IT, ["solo"]) for _ in range(3)] == ["solo"] * 3


def test_no_candidates_returns_none_without_advancing():
    reg = RequestRegistry()
    assert reg.next_assignee(IT, []) is None
    # The empty pick didn't burn a cursor slot — the next real pick starts at 0.
    assert reg.next_assignee(IT, ["a", "b"]) == "a"


def test_round_robin_is_per_namespace():
    reg = RequestRegistry()
    assert reg.next_assignee("helpdesk/it", ["a", "b"]) == "a"
    assert reg.next_assignee("helpdesk/hr", ["x", "y"]) == "x"  # own cursor
    assert reg.next_assignee("helpdesk/it", ["a", "b"]) == "b"


# ── assign / start / complete ─────────────────────────────────────────────────


def test_assign_only_from_unassigned():
    reg = RequestRegistry()
    req = _new(reg)
    assigned = reg.assign(req.id, "op1")
    assert assigned is req and req.state == ASSIGNED and req.assignee == "op1"
    # A second assign is refused — can't steal an in-flight request.
    assert reg.assign(req.id, "op2") is None
    assert req.assignee == "op1"
    assert reg.assign("nonexistent", "op1") is None


def test_start_requires_the_assignee():
    reg = RequestRegistry()
    req = _new(reg)
    reg.assign(req.id, "op1")
    assert reg.start(req.id, "intruder") is None  # not the assignee
    assert req.state == ASSIGNED
    assert reg.start(req.id, "op1") is req and req.state == IN_PROGRESS


def test_complete_ok_and_failed():
    reg = RequestRegistry()
    ok = _new(reg)
    reg.assign(ok.id, "op1")
    reg.start(ok.id, "op1")
    assert reg.complete(ok.id, "op1") is ok and ok.state == DONE and ok.error is None

    bad = _new(reg)
    reg.assign(bad.id, "op1")  # complete straight from assigned (synchronous run)
    done = reg.complete(bad.id, "op1", error="boom")
    assert done is bad and bad.state == FAILED and bad.error == "boom"


def test_complete_refused_for_non_assignee_and_terminal():
    reg = RequestRegistry()
    req = _new(reg)
    reg.assign(req.id, "op1")
    assert reg.complete(req.id, "op2") is None  # not the assignee
    reg.complete(req.id, "op1")  # → done
    assert reg.complete(req.id, "op1") is None  # already terminal


# ── release (AFK timeout) ─────────────────────────────────────────────────────


def test_release_returns_assigned_to_the_queue():
    reg = RequestRegistry()
    req = _new(reg)
    reg.assign(req.id, "op1")
    released = reg.release(req.id)
    assert released is req and req.state == UNASSIGNED and req.assignee is None


def test_release_refused_once_progressing_or_terminal():
    reg = RequestRegistry()
    req = _new(reg)
    reg.assign(req.id, "op1")
    reg.start(req.id, "op1")  # in progress — work has begun
    assert reg.release(req.id) is None
    reg.complete(req.id, "op1")
    assert reg.release(req.id) is None  # terminal


# ── backlog query ─────────────────────────────────────────────────────────────


def test_unassigned_under_filters_by_namespace_and_state():
    reg = RequestRegistry()
    a = _new(reg, namespace="helpdesk/it")
    b = _new(reg, namespace="helpdesk/it")
    other = _new(reg, namespace="helpdesk/hr")
    reg.assign(b.id, "op1")  # b is no longer pending
    pending = reg.unassigned_under("helpdesk/it")
    assert [r.id for r in pending] == [a.id]  # only the still-unassigned IT one
    assert [r.id for r in reg.unassigned_under("helpdesk/hr")] == [other.id]

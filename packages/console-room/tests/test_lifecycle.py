"""
test_lifecycle.py — the conversation state machine (pure, no I/O).
Exercises the open→assigned→resolved→closed transitions, reopen, the refusal
rules, and the change-vs-no-op return contract. See helpdesk-rooms.md §4.
"""

from __future__ import annotations

from console_room.lifecycle import (
    ASSIGNED,
    CLOSED,
    OPEN,
    RESOLVED,
    ConversationRegistry,
)

ALICE = "helpdesk/it/alice@corp"
BOB = "helpdesk/it/bob@corp"


def test_open_creates_then_no_ops_while_live():
    reg = ConversationRegistry()
    conv = reg.open_or_reopen(ALICE)
    assert conv is not None and conv.state == OPEN and conv.assignee is None
    # Already live → a further visitor message is no change.
    assert reg.open_or_reopen(ALICE) is None


def test_claim_assigns_and_is_idempotent_for_same_agent():
    reg = ConversationRegistry()
    reg.open_or_reopen(ALICE)
    conv = reg.claim(ALICE, "agent1")
    assert conv is not None and conv.state == ASSIGNED and conv.assignee == "agent1"
    assert reg.claim(ALICE, "agent1") is None  # same agent re-claim → no change
    # A different agent taking over is a real change (team hand-off).
    conv2 = reg.claim(ALICE, "agent2")
    assert conv2 is not None and conv2.assignee == "agent2"


def test_resolve_keeps_assignee_then_reopen_clears_it():
    reg = ConversationRegistry()
    reg.open_or_reopen(ALICE)
    reg.claim(ALICE, "agent1")
    conv = reg.resolve(ALICE)
    assert conv is not None and conv.state == RESOLVED and conv.assignee == "agent1"
    # A visitor reply reopens → back to the queue (unassigned).
    reopened = reg.open_or_reopen(ALICE)
    assert reopened is not None and reopened.state == OPEN and reopened.assignee is None


def test_reopen_only_from_dormant():
    reg = ConversationRegistry()
    reg.open_or_reopen(ALICE)
    assert reg.reopen(ALICE) is None  # open is already live
    reg.resolve(ALICE)
    conv = reg.reopen(ALICE)
    assert conv is not None and conv.state == OPEN


def test_close_is_terminal_until_reopened():
    reg = ConversationRegistry()
    reg.open_or_reopen(ALICE)
    conv = reg.close(ALICE)
    assert conv is not None and conv.state == CLOSED
    assert reg.close(ALICE) is None  # already closed
    assert reg.claim(ALICE, "agent1") is None  # can't claim a closed thread
    assert reg.resolve(ALICE) is None
    # A visitor reply still rescues it.
    assert reg.open_or_reopen(ALICE).state == OPEN


def test_transitions_on_unknown_room_are_no_ops():
    reg = ConversationRegistry()
    assert reg.resolve(ALICE) is None
    assert reg.reopen(ALICE) is None
    assert reg.close(ALICE) is None
    # claim auto-creates (an agent may grab a thread before the first message).
    assert reg.claim(ALICE, "agent1").state == ASSIGNED


def test_active_under_filters_by_namespace():
    reg = ConversationRegistry()
    reg.open_or_reopen(ALICE)
    reg.open_or_reopen(BOB)
    reg.open_or_reopen("helpdesk/hr/carol@corp")
    rooms = {c.room for c in reg.active_under("helpdesk/it")}
    assert rooms == {ALICE, BOB}

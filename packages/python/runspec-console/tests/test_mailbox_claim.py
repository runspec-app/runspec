"""
test_mailbox_claim.py — the email channel's claim/lifecycle keystone (pure).

The validated fold is the heart of first-ack-wins: every console folding the same
mailbox in the same server order must reach the same verdict. These tests pin the
fold rules and the marker round-trip without any COM — exactly the property that
makes the email channel coordinate without a hub.
"""

from __future__ import annotations

from runspec_console.mailbox_claim import (
    Marker,
    fold_conversation,
    format_marker,
    marker_from_headers,
    owner_of,
    parse_marker,
)


# ── marker format / parse round-trip ──────────────────────────────────────────


def test_marker_round_trips():
    m = Marker(
        kind="claim", queue="it-support", conversation_id="CONV1", agent="ana@web01"
    )
    value = format_marker(m)
    assert value == "it-support/CONV1/ana@web01/claim"
    assert parse_marker(value) == m


def test_parse_marker_rejects_malformed_and_unknown_kind():
    assert parse_marker("") is None
    assert parse_marker("it-support/CONV1/ana@web01") is None  # too few fields
    assert parse_marker("it-support/CONV1/ana@web01/bogus") is None  # unknown kind
    assert parse_marker("it-support//ana@web01/claim") is None  # empty conversation


def test_marker_from_headers():
    blob = (
        "To: it-support@corp.com\r\n"
        "X-Runspec-Claim: it-support/CONV1/ana@web01/claim\r\n"
        "Subject: RE: help\r\n"
    )
    assert marker_from_headers(blob) == "it-support/CONV1/ana@web01/claim"


def test_marker_from_headers_absent_or_empty():
    assert marker_from_headers("To: it-support@corp.com\r\nSubject: help\r\n") is None
    assert marker_from_headers("") is None


# ── the validated fold ────────────────────────────────────────────────────────


def _claim(order, agent):
    return {"order_key": order, "kind": "claim", "agent": agent}


def _life(order, kind, agent):
    return {"order_key": order, "kind": kind, "agent": agent}


def _msg(order, from_requester=True):
    return {"order_key": order, "kind": "message", "from_requester": from_requester}


def test_unclaimed_conversation_is_open():
    assert fold_conversation([_msg(1)]) == {"state": "open", "owner": None}
    assert fold_conversation([]) == {"state": "open", "owner": None}


def test_first_ack_wins_regardless_of_insertion_order():
    # ana's ack has the earlier server order; bob's came later. Whatever order we
    # feed them, the fold agrees: ana owns it.
    events = [_claim(2, "bob@web02"), _claim(1, "ana@web01")]
    assert fold_conversation(events) == {"state": "assigned", "owner": "ana@web01"}
    assert owner_of(list(reversed(events))) == "ana@web01"


def test_resolve_only_counts_from_owner():
    events = [
        _claim(1, "ana@web01"),
        _life(2, "resolve", "bob@web02"),  # a non-owner can't resolve
    ]
    assert fold_conversation(events) == {"state": "assigned", "owner": "ana@web01"}
    events.append(_life(3, "resolve", "ana@web01"))  # the owner can
    assert fold_conversation(events) == {"state": "resolved", "owner": "ana@web01"}


def test_close_then_reopen_by_owner():
    events = [
        _claim(1, "ana@web01"),
        _life(2, "close", "ana@web01"),
        _life(3, "reopen", "ana@web01"),
    ]
    assert fold_conversation(events) == {"state": "assigned", "owner": "ana@web01"}


def test_reopen_ignored_from_non_owner():
    events = [
        _claim(1, "ana@web01"),
        _life(2, "resolve", "ana@web01"),
        _life(3, "reopen", "bob@web02"),  # not the owner → ignored
    ]
    assert fold_conversation(events)["state"] == "resolved"


def test_requester_reply_after_resolve_reopens():
    events = [
        _claim(1, "ana@web01"),
        _life(2, "resolve", "ana@web01"),
        _msg(3, from_requester=True),  # customer comes back → reopened, still ana's
    ]
    assert fold_conversation(events) == {"state": "assigned", "owner": "ana@web01"}


def test_requester_reply_before_any_claim_stays_open():
    assert fold_conversation([_msg(1), _msg(2)]) == {"state": "open", "owner": None}


def test_two_agents_resolving_is_idempotent():
    events = [
        _claim(1, "ana@web01"),
        _life(2, "resolve", "ana@web01"),
        _life(3, "resolve", "ana@web01"),  # harmless repeat
    ]
    assert fold_conversation(events) == {"state": "resolved", "owner": "ana@web01"}


def test_order_key_breaks_same_instant_tie():
    # Two acks at the same instant; the internetMessageId tiebreak in the order
    # key decides — deterministically, the same on every console.
    events = [
        {"order_key": (100, "<z@corp>"), "kind": "claim", "agent": "bob@web02"},
        {"order_key": (100, "<a@corp>"), "kind": "claim", "agent": "ana@web01"},
    ]
    assert owner_of(events) == "ana@web01"  # lower message-id wins the tie

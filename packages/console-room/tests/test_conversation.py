"""
test_conversation.py — the pure naming / namespace-matching helpers for helpdesk
conversations (no I/O). See docs/design/helpdesk-rooms.md §3.
"""

from __future__ import annotations

from console_room.conversation import (
    conversation_room,
    namespace_matches,
    parse_conversation_room,
    queue_namespace,
    valid_token,
)


# ── naming ────────────────────────────────────────────────────────────────────


def test_conversation_room_shape():
    assert (
        conversation_room("it-support", "alice@corp")
        == "helpdesk/it-support/alice@corp"
    )


def test_queue_namespace_shape():
    assert queue_namespace("it-support") == "helpdesk/it-support"


def test_conversation_room_is_under_its_namespace():
    room = conversation_room("it-support", "alice@corp")
    assert namespace_matches(queue_namespace("it-support"), room)


def test_round_trip():
    room = conversation_room("hr", "bob@corp")
    assert parse_conversation_room(room) == ("hr", "bob@corp")


def test_parse_rejects_non_conversation_rooms():
    assert parse_conversation_room("ops") is None  # a group-chat room
    assert parse_conversation_room("helpdesk/it") is None  # no visitor segment
    assert parse_conversation_room("helpdesk/it/alice/extra") is None  # too deep
    assert parse_conversation_room("other/it/alice") is None  # wrong prefix


# ── namespace matching (the isolation boundary) ────────────────────────────────


def test_namespace_matches_on_segment_boundary():
    ns = queue_namespace("it")
    assert namespace_matches(ns, "helpdesk/it/alice@corp")
    assert namespace_matches(ns, ns)  # the namespace itself
    # Must not bleed across a queue whose name merely starts with another's.
    assert not namespace_matches(ns, "helpdesk/it-eu/bob@corp")


def test_namespace_does_not_match_other_queue():
    assert not namespace_matches(
        queue_namespace("hr"), conversation_room("it", "alice@corp")
    )


def test_namespace_does_not_match_group_chat_room():
    assert not namespace_matches(queue_namespace("it"), "ops")


# ── token validation ───────────────────────────────────────────────────────────


def test_valid_token_accepts_identities():
    assert valid_token("alice@corp")
    assert valid_token("it-support")
    assert valid_token("user.name+tag_1")


def test_valid_token_rejects_structure_breakers():
    assert not valid_token("")  # empty
    assert not valid_token("a/b")  # separator → could escape the namespace
    assert not valid_token("a b")  # whitespace
    assert not valid_token("a\tb")

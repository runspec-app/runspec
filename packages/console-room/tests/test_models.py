"""
test_models.py — the wire schema: message stamping, frame shapes, hello parsing.
Pure functions, no I/O.
"""

from __future__ import annotations

from console_room.models import (
    TEXT_CAP,
    Message,
    normalize_status,
    parse_hello,
    presence_frame,
)


def test_message_new_stamps_id_and_ts():
    m = Message.new("ops", "alice@web01", "hi")
    assert m.room == "ops" and m.sender == "alice@web01" and m.text == "hi"
    assert m.id and isinstance(m.ts, float) and m.ts > 0


def test_message_text_is_capped():
    m = Message.new("ops", "a", "x" * (TEXT_CAP + 50))
    assert len(m.text) == TEXT_CAP + 1 and m.text.endswith("…")


def test_message_to_frame_shape():
    m = Message(room="ops", sender="a", text="hi", id="ID1", ts=1.5)
    assert m.to_frame() == {
        "type": "message",
        "room": "ops",
        "sender": "a",
        "text": "hi",
        "id": "ID1",
        "ts": 1.5,
    }


def test_presence_frame_normalises_status():
    assert presence_frame("ops", "a", "available")["status"] == "available"
    assert presence_frame("ops", "a", "whatever")["status"] == "away"
    assert presence_frame("ops", "a", "away") == {
        "type": "presence",
        "room": "ops",
        "user": "a",
        "status": "away",
    }


def test_normalize_status():
    assert normalize_status("available") == "available"
    assert normalize_status("away") == "away"
    assert normalize_status(None) == "away"


def test_parse_hello_ok():
    h = parse_hello({"type": "hello", "room": "ops", "user": "alice@web01"})
    assert h is not None and h.room == "ops" and h.user == "alice@web01"


def test_parse_hello_blank_user_falls_back():
    h = parse_hello({"type": "hello", "room": "ops", "user": "  "})
    assert h is not None and h.user == "unknown"


def test_parse_hello_rejects_missing_room_and_wrong_type():
    assert parse_hello({"type": "hello", "user": "a"}) is None
    assert parse_hello({"type": "hello", "room": "", "user": "a"}) is None
    assert parse_hello({"type": "message", "room": "ops"}) is None
    assert parse_hello("nope") is None

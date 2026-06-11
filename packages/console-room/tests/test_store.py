"""
test_store.py — the SQLite transcript: append, recent ordering/limit, idempotency,
and on-disk durability across reopen.
"""

from __future__ import annotations

from console_room.models import Message
from console_room.store import TranscriptStore


def _msg(room, text, ts, id_):
    return Message(room=room, sender="a", text=text, id=id_, ts=ts)


def test_append_and_recent_chronological():
    s = TranscriptStore(":memory:")
    s.append(_msg("ops", "first", 1.0, "i1"))
    s.append(_msg("ops", "second", 2.0, "i2"))
    s.append(_msg("ops", "third", 3.0, "i3"))
    got = [m.text for m in s.recent("ops", 10)]
    assert got == ["first", "second", "third"]  # oldest-first


def test_recent_limit_keeps_newest():
    s = TranscriptStore(":memory:")
    for i in range(5):
        s.append(_msg("ops", f"m{i}", float(i), f"i{i}"))
    got = [m.text for m in s.recent("ops", 2)]
    assert got == ["m3", "m4"]  # newest two, still chronological


def test_recent_scopes_to_room():
    s = TranscriptStore(":memory:")
    s.append(_msg("ops", "a", 1.0, "i1"))
    s.append(_msg("infra", "b", 2.0, "i2"))
    assert [m.text for m in s.recent("ops", 10)] == ["a"]
    assert [m.text for m in s.recent("infra", 10)] == ["b"]


def test_recent_zero_limit_is_empty():
    s = TranscriptStore(":memory:")
    s.append(_msg("ops", "a", 1.0, "i1"))
    assert s.recent("ops", 0) == []


def test_append_idempotent_on_id():
    s = TranscriptStore(":memory:")
    s.append(_msg("ops", "a", 1.0, "dup"))
    s.append(_msg("ops", "a-again", 2.0, "dup"))  # same id → ignored
    assert [m.text for m in s.recent("ops", 10)] == ["a"]


def test_durable_across_reopen(tmp_path):
    db = tmp_path / "t.db"
    s = TranscriptStore(db)
    s.append(_msg("ops", "kept", 1.0, "i1"))
    s.close()
    s2 = TranscriptStore(db)
    assert [m.text for m in s2.recent("ops", 10)] == ["kept"]
    s2.close()

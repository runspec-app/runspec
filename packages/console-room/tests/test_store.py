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


def test_request_kind_and_data_round_trip():
    s = TranscriptStore(":memory:")
    req = Message(
        room="helpdesk/it/alice@corp",
        sender="alice@corp",
        text="Requested: reset-password — user=alice",
        id="r1",
        ts=1.0,
        kind="request",
        data={"tool": "reset-password", "args": {"user": "alice"}},
    )
    s.append(req)
    s.append(_msg("helpdesk/it/alice@corp", "plain chat", 2.0, "c1"))
    got = s.recent("helpdesk/it/alice@corp", 10)
    assert got[0].kind == "request"
    assert got[0].data == {"tool": "reset-password", "args": {"user": "alice"}}
    assert got[1].kind == "chat" and got[1].data is None


def test_migrates_pre_helpdesk_schema_in_place(tmp_path):
    # A transcript created before the kind/data columns must open cleanly and
    # report its old rows as plain chat.
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, room TEXT NOT NULL, "
        "sender TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO messages VALUES ('i1', 'ops', 'a', 'from the old days', 1.0)"
    )
    conn.commit()
    conn.close()

    s = TranscriptStore(db)  # must ALTER in the new columns
    got = s.recent("ops", 10)
    assert [m.text for m in got] == ["from the old days"]
    assert got[0].kind == "chat" and got[0].data is None
    # And new-style rows persist alongside the migrated ones.
    s.append(
        Message(
            room="ops",
            sender="a",
            text="new",
            id="i2",
            ts=2.0,
            kind="request",
            data={"tool": "x", "args": {}},
        )
    )
    assert s.recent("ops", 10)[-1].data == {"tool": "x", "args": {}}
    s.close()

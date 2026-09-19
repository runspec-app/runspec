"""
test_conversations.py — saved chat conversations (persistence + bridge methods).

Covers the pure ``conversations`` store (append/list/load/delete/rename/prune)
and the Bridge methods that wrap it (persist a turn, list/load/resume/delete/
rename), with the store pointed at a tmp dir.
"""

from __future__ import annotations

import threading

import runspec_console.config as config_mod
from runspec_console import conversations as conv
from runspec_console.bridge import Bridge
from tests.session_stub import current as _S, install_sessions


# ── pure store ──────────────────────────────────────────────────────────────────


def _turn(user, assistant="", tools=None):
    return {"user": user, "assistant": assistant, "tools": tools or [], "ts": "t"}


def test_append_creates_then_appends_and_titles(tmp_path):
    cid = conv.new_id()
    conv.append_turn(tmp_path, cid, _turn("first question", "an answer"))
    conv.append_turn(tmp_path, cid, _turn("second question"))
    rec = conv.load(tmp_path, cid)
    assert rec is not None
    assert rec["turn_count"] == 2
    assert rec["title"] == "first question"  # title comes from the first user msg
    assert [t["user"] for t in rec["turns"]] == ["first question", "second question"]


def test_list_meta_newest_first_with_preview(tmp_path):
    a = conv.new_id()
    b = conv.new_id()
    conv.append_turn(tmp_path, a, _turn("older", "old answer"))
    conv.append_turn(tmp_path, b, _turn("newer", "new answer"))
    metas = conv.list_meta(tmp_path)
    assert [m["id"] for m in metas] == [b, a]  # newest first
    assert metas[0]["preview"] == "new answer"
    assert metas[0]["turn_count"] == 1


def test_delete_and_rename(tmp_path):
    cid = conv.new_id()
    conv.append_turn(tmp_path, cid, _turn("q", "a"))
    assert conv.rename(tmp_path, cid, "My chat") is True
    assert conv.load(tmp_path, cid)["title"] == "My chat"
    conv.delete(tmp_path, cid)
    assert conv.load(tmp_path, cid) is None
    assert conv.rename(tmp_path, cid, "x") is False  # gone


def test_prune_by_count_keeps_newest(tmp_path):
    ids = []
    for i in range(5):
        cid = conv.new_id()
        conv.append_turn(tmp_path, cid, _turn(f"q{i}"))
        ids.append(cid)
    removed = conv.prune(tmp_path, max_conversations=2)
    assert removed == 3
    kept = {m["id"] for m in conv.list_meta(tmp_path)}
    assert kept == set(ids[-2:])  # the two most recently updated


def test_safe_id_rejects_traversal(tmp_path):
    # A malicious id can't escape the conversations dir.
    assert conv.load(tmp_path, "../secret") is None
    conv.delete(tmp_path, "../secret")  # no raise


# ── operator-only records (manual runs) ──────────────────────────────────────────


def _op_run(runnable="deploy", host="web01", exit_code=0):
    return {
        "kind": "operator_run",
        "runnable": runnable,
        "host": host,
        "args": {"env": "prod"},
        "output": "line1\nline2",
        "exit_code": exit_code,
        "duration_ms": 42,
        "operator": "alice",
        "ts": "t",
    }


def test_is_chat_turn_and_chat_turns_filter():
    chat = _turn("hi")
    op = _op_run()
    assert conv.is_chat_turn(chat) is True
    assert conv.is_chat_turn({"user": "x"}) is True  # kind absent ⇒ chat
    assert conv.is_chat_turn(op) is False
    assert conv.chat_turns([chat, op, chat]) == [chat, chat]


def test_operator_run_title_and_preview(tmp_path):
    cid = conv.new_id()
    conv.append_turn(tmp_path, cid, _op_run(runnable="deploy", host="web01"))
    meta = conv.list_meta(tmp_path)[0]
    assert meta["title"] == "Ran deploy on web01"  # sensible title from a run
    assert meta["preview"] == "ran deploy → exit 0"


def test_turns_to_text_skips_operator_records():
    text = conv.turns_to_text([_turn("q", "a"), _op_run(runnable="secret-run")])
    assert "User: q" in text and "Assistant: a" in text
    assert "secret-run" not in text  # operator run never reaches the summary input


# ── bridge methods ──────────────────────────────────────────────────────────────


class _ConvBridge:
    list_conversations = Bridge.list_conversations
    load_conversation = Bridge.load_conversation
    resume_conversation = Bridge.resume_conversation
    delete_conversation = Bridge.delete_conversation
    rename_conversation = Bridge.rename_conversation
    _persist_conversation_turn = Bridge._persist_conversation_turn
    _persist_operator_run = Bridge._persist_operator_run
    _flatten_turns = staticmethod(Bridge._flatten_turns)
    _mechanical_resume_note = staticmethod(Bridge._mechanical_resume_note)
    _summarize_middle_turns = Bridge._summarize_middle_turns

    def __init__(self):
        self._lock = threading.Lock()
        install_sessions(self)

    def get_config(self):
        return {"llm": {"provider": "anthropic"}}

    def _get_adapter(self):
        return None  # no model in unit tests → summary falls back to the note


def _point_store(
    monkeypatch, tmp_path, *, enabled=True, keep_first=1, keep_recent=20, compact=False
):
    monkeypatch.setattr(config_mod, "conversations_dir", lambda: tmp_path)
    monkeypatch.setattr(
        config_mod,
        "read_chat_history",
        lambda: {
            "enabled": enabled,
            "max_conversations": 0,
            "max_age_days": 0,
            "resume_keep_first": keep_first,
            "resume_keep_recent": keep_recent,
            "compact_on_resume": compact,
        },
    )


def test_persist_turn_writes_and_mints_id(tmp_path, monkeypatch):
    _point_store(monkeypatch, tmp_path)
    b = _ConvBridge()
    b._persist_conversation_turn(
        _S(b), "hello", "hi there", [], {"input": 1, "output": 2}, "m"
    )
    assert b._chat_session_id  # the session id is the conversation id
    metas = b.list_conversations()
    assert len(metas) == 1 and metas[0]["title"] == "hello"
    rec = b.load_conversation(b._chat_session_id)
    assert rec["turns"][0]["assistant"] == "hi there"
    assert rec["turns"][0]["usage"] == {"input": 1, "output": 2}


def test_persist_disabled_writes_nothing(tmp_path, monkeypatch):
    _point_store(monkeypatch, tmp_path, enabled=False)
    b = _ConvBridge()
    b._persist_conversation_turn(_S(b), "hello", "hi", [], {}, "m")
    assert b.list_conversations() == []  # nothing written


def test_resume_rebuilds_history_and_sets_active(tmp_path, monkeypatch):
    _point_store(monkeypatch, tmp_path)
    cid = conv.new_id()
    conv.append_turn(
        tmp_path,
        cid,
        _turn("run the report", "done", tools=[{"name": "rep", "output": "ok"}]),
    )
    b = _ConvBridge()
    res = b.resume_conversation(cid)
    assert res["ok"] and res["turns"] == 1 and res["exact"] is False
    assert b._chat_session_id == cid  # new turns continue this conversation
    roles = [m["role"] for m in _S(b).history]
    assert roles == ["user", "assistant"]
    assert _S(b).history[0]["content"] == "run the report"
    assert "done" in _S(b).history[1]["content"]
    assert "[ran rep" in _S(b).history[1]["content"]  # tool folded into text


def test_resume_missing_returns_error(tmp_path, monkeypatch):
    _point_store(monkeypatch, tmp_path)
    b = _ConvBridge()
    assert b.resume_conversation("nope")["ok"] is False


def test_persist_operator_run_writes_and_mints_id(tmp_path, monkeypatch):
    _point_store(monkeypatch, tmp_path)
    b = _ConvBridge()
    b._persist_operator_run(
        runnable="deploy",
        host="web01",
        args={"env": "prod"},
        output="out",
        exit_code=0,
        duration_ms=7,
        operator="alice",
    )
    assert b._chat_session_id  # a session (== conversation) opened for it
    rec = b.load_conversation(b._chat_session_id)
    op = rec["turns"][0]
    assert op["kind"] == "operator_run" and op["runnable"] == "deploy"
    assert op["output"] == "out" and op["operator"] == "alice"


def test_operator_run_never_replayed_to_the_agent(tmp_path, monkeypatch):
    """The core guarantee: an operator run renders for the operator (it's in the
    stored turns) but is absent from the model history a resume rebuilds."""
    _point_store(monkeypatch, tmp_path)
    cid = conv.new_id()
    conv.append_turn(tmp_path, cid, _turn("do a thing", "did it"))
    conv.append_turn(tmp_path, cid, _op_run(runnable="secret-deploy"))
    b = _ConvBridge()
    res = b.resume_conversation(cid)
    # Both records are still on disk for the operator UI to render …
    assert len(b.load_conversation(cid)["turns"]) == 2
    # … but only the chat exchange is in the rebuilt model history.
    assert res["kept"] == 1
    text = "\n".join(m["content"] for m in _S(b).history)
    assert "did it" in text
    assert "secret-deploy" not in text


def test_flatten_turns_skips_operator_and_denied(tmp_path):
    turns = [
        _turn(
            "q",
            "a",
            tools=[
                {"name": "ok-tool", "output": "fine"},
                {"name": "nope", "output": "x", "operator_decision": "denied"},
            ],
        ),
        _op_run(runnable="manual-thing"),
    ]
    msgs = Bridge._flatten_turns(turns)
    assert [m["role"] for m in msgs] == ["user", "assistant"]  # operator run dropped
    text = msgs[1]["content"]
    assert "[ran ok-tool" in text
    assert "nope" not in text  # a denied call is not replayed as if it ran
    assert "manual-thing" not in text


def test_delete_and_rename_via_bridge(tmp_path, monkeypatch):
    _point_store(monkeypatch, tmp_path)
    b = _ConvBridge()
    b._persist_conversation_turn(_S(b), "q", "a", [], {}, "m")
    cid = b._chat_session_id
    b.rename_conversation(cid, "Renamed")
    assert b.load_conversation(cid)["title"] == "Renamed"
    b.delete_conversation(cid)
    assert b.load_conversation(cid) == {}


# ── resume-time compaction ──────────────────────────────────────────────────────


def test_split_for_compaction():
    turns = [_turn(f"q{i}") for i in range(10)]
    # Fits → everything verbatim, nothing condensed.
    head, middle, recent = conv.split_for_compaction(
        turns, keep_first=1, keep_recent=20
    )
    assert (head, middle, recent) == (turns, [], [])
    # Overflows → opening kept, recent kept, the rest is the middle.
    head, middle, recent = conv.split_for_compaction(turns, keep_first=1, keep_recent=3)
    assert [t["user"] for t in head] == ["q0"]
    assert [t["user"] for t in recent] == ["q7", "q8", "q9"]
    assert len(middle) == 6
    # keep_recent <= 0 disables compaction.
    assert conv.split_for_compaction(turns, keep_recent=0) == (turns, [], [])


def _seed(tmp_path, n):
    cid = conv.new_id()
    for i in range(n):
        conv.append_turn(tmp_path, cid, _turn(f"question {i}", f"answer {i}"))
    return cid


def test_resume_compacts_long_conversation_with_note(tmp_path, monkeypatch):
    _point_store(monkeypatch, tmp_path, keep_recent=3)
    cid = _seed(tmp_path, 10)
    b = _ConvBridge()
    res = b.resume_conversation(cid)
    assert res["compacted"] is True and res["summarized"] is False
    assert res["turns"] == 10 and res["kept"] == 4  # 1 opening + 3 recent
    text = "\n".join(m["content"] for m in _S(b).history)
    assert "earlier turns were condensed" in text  # the mechanical note (path A)
    assert "answer 0" in text and "answer 9" in text  # opening + recent kept verbatim
    # A middle turn's answer was dropped (the note lists middle *questions* only,
    # so check an answer — which never appears in the note).
    assert "answer 5" not in text


def test_resume_short_conversation_not_compacted(tmp_path, monkeypatch):
    _point_store(monkeypatch, tmp_path, keep_recent=20)
    cid = _seed(tmp_path, 3)
    b = _ConvBridge()
    res = b.resume_conversation(cid)
    assert res["compacted"] is False and res["kept"] == 3
    assert all("condensed" not in m["content"] for m in _S(b).history)


def test_resume_compact_on_resume_falls_back_without_model(tmp_path, monkeypatch):
    # compact_on_resume=True but no adapter → falls back to the mechanical note,
    # summarized=False (never raises, never blocks the resume).
    _point_store(monkeypatch, tmp_path, keep_recent=2, compact=True)
    cid = _seed(tmp_path, 8)
    b = _ConvBridge()
    res = b.resume_conversation(cid)
    assert res["compacted"] is True and res["summarized"] is False
    assert any("earlier turns were condensed" in m["content"] for m in _S(b).history)


def test_resume_keep_first_is_tunable(tmp_path, monkeypatch):
    # keep_first=2 keeps the first two turns verbatim (their answers survive).
    _point_store(monkeypatch, tmp_path, keep_first=2, keep_recent=2)
    cid = _seed(tmp_path, 8)
    b = _ConvBridge()
    res = b.resume_conversation(cid)
    assert res["compacted"] is True and res["kept"] == 4  # 2 opening + 2 recent
    text = "\n".join(m["content"] for m in _S(b).history)
    assert "answer 0" in text and "answer 1" in text  # both opening turns verbatim
    assert "answer 2" not in text  # turn 2 fell into the condensed middle


def test_resume_keep_first_zero_keeps_no_opening(tmp_path, monkeypatch):
    _point_store(monkeypatch, tmp_path, keep_first=0, keep_recent=2)
    cid = _seed(tmp_path, 8)
    b = _ConvBridge()
    res = b.resume_conversation(cid)
    assert res["compacted"] is True and res["kept"] == 2  # recent only
    assert _S(b).history[0]["role"] == "user"  # still starts on a user turn


def test_resume_history_stays_alternating_after_compaction(tmp_path, monkeypatch):
    # The condensed note must not break strict user/assistant alternation
    # (Anthropic requires it) — it's folded into the first recent user message.
    _point_store(monkeypatch, tmp_path, keep_recent=3)
    cid = _seed(tmp_path, 12)
    b = _ConvBridge()
    b.resume_conversation(cid)
    roles = [m["role"] for m in _S(b).history]
    assert roles[0] == "user"
    assert all(a != b2 for a, b2 in zip(roles, roles[1:]))  # strictly alternating

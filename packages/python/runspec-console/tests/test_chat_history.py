"""
test_chat_history.py — the agent chat must remember prior turns (the model sees
earlier user/assistant/tool messages), clear_chat resets it, and the rolling
history is trimmed safely (never orphaning a tool_result).
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from runspec_console.adapters.base import ToolCall
from runspec_console.bridge import Bridge


def _resp(stop_reason, tool_calls, inp=10, out=5):
    return SimpleNamespace(
        stop_reason=stop_reason,
        tool_calls=tool_calls,
        _raw=SimpleNamespace(
            usage=SimpleNamespace(input_tokens=inp, output_tokens=out)
        ),
    )


class _StubBridge:
    def __init__(self):
        self._lock = threading.Lock()
        self._chat_history: list[dict] = []
        self._history_turn_ids: list[str] = []
        self._excluded_turns: set[str] = set()
        self._chat_lock = threading.Lock()
        self._chat_cancels: dict[str, threading.Event] = {}
        self._pending_confirms: dict = {}
        self._in_flight: dict = {}
        self._conversation_id: str = ""
        self.dispatched: list = []

    def _dispatch(self, event, detail):
        self.dispatched.append((event, detail))

    # Saved-conversation persistence is exercised in test_conversations.py; these
    # in-memory-history tests stub it out so they don't touch the disk store.
    def _persist_conversation_turn(self, *a, **k):
        pass

    # Audio cue + window-focus helpers the chat turn calls (run-outcome / reply
    # cues); no-ops in these unit tests.
    def _play_sound(self, category, *, force=False):
        pass

    def _window_is_foreground(self):
        return False

    def _runnables_to_tools(self):
        return []

    def get_config(self):
        return {}  # tool_mode defaults to dedup → no catalog index in these tests

    async def _gated_run_tool_async(self, name, tool_input, chat_id):
        return f"ran:{name}"

    _tool_mode = Bridge._tool_mode
    _fold_catalog_index = Bridge._fold_catalog_index

    def _chat_index_text(self):
        return ""  # dedup-mode stub: no folded menu

    _debug = False  # tracing off → _trace is a no-op in these unit tests
    _usage_from_response = staticmethod(Bridge._usage_from_response)
    _messages_chars = staticmethod(Bridge._messages_chars)
    _trim_chat_history = Bridge._trim_chat_history
    _sync_history_tags = Bridge._sync_history_tags
    _visible_history = Bridge._visible_history
    _drop_turns = Bridge._drop_turns
    set_turn_included = Bridge.set_turn_included
    _trace = Bridge._trace
    clear_chat = Bridge.clear_chat
    cancel_chat = Bridge.cancel_chat
    compact_chat = Bridge.compact_chat
    _finish_compact = Bridge._finish_compact
    _compact_note = Bridge._compact_note
    _agentic_chat_turn = Bridge._agentic_chat_turn


class _TextAdapter:
    """Replies with one text turn; records the history it was handed each call."""

    def __init__(self, reply="ok"):
        self.reply = reply
        self.seen: list[list[dict]] = []

    async def stream_with_tools(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        yield ("text", self.reply)
        yield ("done", _resp("end_turn", []))

    def make_tool_turn(self, response, results):  # pragma: no cover - unused here
        return []


# ── conversational memory ──────────────────────────────────────────────


def test_history_persists_across_turns():
    b, ad = _StubBridge(), _TextAdapter("hi there")
    asyncio.run(b._agentic_chat_turn("c1", "first question", ad))
    asyncio.run(b._agentic_chat_turn("c2", "second question", ad))

    # The second turn must have been handed the first turn's user + assistant.
    second_seen = ad.seen[1]
    contents = [(m["role"], m["content"]) for m in second_seen]
    assert ("user", "first question") in contents
    assert ("assistant", "hi there") in contents
    assert ("user", "second question") in contents
    # Persisted: user1, assistant1, user2, assistant2
    assert [m["role"] for m in b._chat_history] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_clear_chat_starts_fresh():
    b, ad = _StubBridge(), _TextAdapter()
    asyncio.run(b._agentic_chat_turn("c1", "remember this", ad))
    assert b._chat_history
    b.clear_chat()
    assert b._chat_history == []

    asyncio.run(b._agentic_chat_turn("c2", "new topic", ad))
    # The post-clear turn only sees its own message, not the old one.
    assert ad.seen[-1] == [{"role": "user", "content": "new topic"}]


# ── tool-calling turns are persisted too ───────────────────────────────


class _ToolThenText:
    def __init__(self):
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def stream_with_tools(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        self.calls += 1
        if self.calls == 1:
            tc = ToolCall(id="t1", name="local__svc", input={"a": 1})
            yield ("done", _resp("tool_use", [tc]))
        else:
            yield ("text", "all done")
            yield ("done", _resp("end_turn", []))

    def make_tool_turn(self, response, results):
        return [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": results[0][1],
                    }
                ],
            },
        ]


def test_tool_turn_round_trip_persisted():
    b, ad = _StubBridge(), _ToolThenText()
    asyncio.run(b._agentic_chat_turn("c1", "do it", ad))
    # user, assistant(tool_use), user(tool_result), assistant(text)
    assert [m["role"] for m in b._chat_history] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert b._chat_history[-1] == {"role": "assistant", "content": "all done"}
    # The 2nd model call saw the tool_result from the 1st.
    assert ad.calls == 2
    assert any(
        isinstance(m.get("content"), list)
        and m["content"][0].get("type") == "tool_result"
        for m in ad.seen[1]
    )


# ── stop / cancel ───────────────────────────────────────────────────────


class _CancelAfterText:
    """Streams some text, then (after the bridge has dispatched it) the test has
    flipped the cancel event, so the turn must stop before running any tool."""

    def __init__(self, cancel: threading.Event):
        self.cancel = cancel
        self.ran_tool = False

    async def stream_with_tools(self, messages, tools):
        yield ("text", "thinking…")
        self.cancel.set()  # user hits Stop mid-stream
        yield ("text", " more")
        tc = ToolCall(id="t1", name="local__danger", input={})
        yield ("done", _resp("tool_use", [tc]))

    def make_tool_turn(self, response, results):
        self.ran_tool = True  # must NOT be reached when cancelled
        return []


def test_cancel_stops_before_running_tools():
    b = _StubBridge()
    ev = threading.Event()
    b._chat_cancels["c1"] = ev
    ad = _CancelAfterText(ev)
    asyncio.run(b._agentic_chat_turn("c1", "go", ad))

    # No tool turn was built → no host action taken.
    assert ad.ran_tool is False
    # A "Stopped" notice was surfaced to the UI.
    assert any(
        ev_name == "runspec:token" and "Stopped" in detail.get("token", "")
        for ev_name, detail in b.dispatched
    )
    # History holds the user msg + the partial assistant text (no dangling tool_use).
    assert [m["role"] for m in b._chat_history] == ["user", "assistant"]


def test_cancel_before_turn_runs_nothing():
    b = _StubBridge()
    ev = threading.Event()
    ev.set()  # already cancelled when the turn starts
    b._chat_cancels["c1"] = ev
    ad = _TextAdapter("should not appear")
    asyncio.run(b._agentic_chat_turn("c1", "go", ad))
    assert ad.seen == []  # model never called
    assert [m["role"] for m in b._chat_history] == ["user"]


def test_cancel_chat_sets_the_event():
    b = _StubBridge()
    ev = threading.Event()
    b._chat_cancels["c1"] = ev
    b.cancel_chat("c1")
    assert ev.is_set()
    b.cancel_chat("nonexistent")  # must not raise


# ── trimming ────────────────────────────────────────────────────────────


def test_trim_caps_and_keeps_user_start():
    b = _StubBridge()
    b._chat_history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(100)
    ]
    b._trim_chat_history(max_messages=10)
    assert len(b._chat_history) <= 10
    first = b._chat_history[0]
    assert first["role"] == "user" and isinstance(first["content"], str)


def test_trim_drops_orphaned_tool_result_prefix():
    b = _StubBridge()
    b._chat_history = [
        {"role": "user", "content": "q0"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]},
        {"role": "assistant", "content": "mid"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "final"},
    ]
    b._trim_chat_history(max_messages=4)
    # After dropping the oldest 2, the slice would start on an orphaned
    # tool_result — trim must skip forward to the next real user message.
    assert b._chat_history[0] == {"role": "user", "content": "q1"}
    assert [m["role"] for m in b._chat_history] == ["user", "assistant"]


def test_trim_noop_under_cap():
    b = _StubBridge()
    b._chat_history = [{"role": "user", "content": "x"}]
    b._trim_chat_history(max_messages=60)
    assert b._chat_history == [{"role": "user", "content": "x"}]


# ── compact_chat (live in-session compaction) ───────────────────────────────


def _long_history(pairs: int) -> list[dict]:
    """`pairs` user/assistant exchanges as a flat message list."""
    hist: list[dict] = []
    for i in range(pairs):
        hist.append({"role": "user", "content": f"q{i}"})
        hist.append({"role": "assistant", "content": f"a{i}"})
    return hist


def test_compact_drops_prefix_and_folds_note(monkeypatch):
    import runspec_console.config as cfg

    monkeypatch.setattr(cfg, "read_chat_history", lambda: {"resume_keep_recent": 4})
    b = _StubBridge()
    b._chat_history = _long_history(10)  # 20 messages
    res = b.compact_chat()

    assert res["ok"] and res["compacted"] > 0
    hist = b._chat_history
    # Kept tail is the recent messages; it still starts on a user message so
    # alternation/pairing is intact.
    assert hist[0]["role"] == "user"
    assert len(hist) <= 4 + 1  # keep_recent tail, possibly +1 from the boundary walk
    # The note summarising the dropped prefix is folded into the first kept user
    # message (not a separate message that would break alternation).
    assert "compacted to free up context" in hist[0]["content"]
    assert "q0" in hist[0]["content"]  # an early question is named in the note
    # The compaction event fired for the gauge.
    assert any(e == "runspec:chat_compacted" for e, _ in b.dispatched)


def test_compact_noop_when_short(monkeypatch):
    import runspec_console.config as cfg

    monkeypatch.setattr(cfg, "read_chat_history", lambda: {"resume_keep_recent": 20})
    b = _StubBridge()
    b._chat_history = _long_history(3)  # 6 messages, under keep_recent + 2
    before = list(b._chat_history)
    res = b.compact_chat()

    assert res == {"ok": True, "compacted": 0}
    assert b._chat_history == before  # untouched
    assert not any(e == "runspec:chat_compacted" for e, _ in b.dispatched)


def test_compact_preserves_tool_pairing(monkeypatch):
    import runspec_console.config as cfg

    monkeypatch.setattr(cfg, "read_chat_history", lambda: {"resume_keep_recent": 2})
    b = _StubBridge()
    # A tool_use/tool_result pair sits right at the prospective cut; the cut must
    # walk forward to the next plain user message so the pair is never split.
    b._chat_history = [
        {"role": "user", "content": "q0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    b.compact_chat()
    hist = b._chat_history
    # Never starts on an orphaned tool_result; first message is a plain user str.
    assert hist[0]["role"] == "user" and isinstance(hist[0]["content"], str)


# ── "in context" toggle: exclude / re-include a chat turn ────────────────────


def test_excluded_turn_hidden_from_model_but_kept_in_history():
    """set_turn_included(False) drops a turn from what the model sees
    (_visible_history) without deleting it — so re-including restores it."""
    b, ad = _StubBridge(), _TextAdapter("reply")
    asyncio.run(b._agentic_chat_turn("c1", "first", ad))
    asyncio.run(b._agentic_chat_turn("c2", "second", ad))
    # Tags cover every message, one per turn.
    assert len(b._history_turn_ids) == len(b._chat_history)
    assert set(b._history_turn_ids) == {"c1", "c2"}

    # Exclude the first turn.
    res = b.set_turn_included("c1", included=False)
    assert res == {"ok": True, "included": False}
    visible = b._visible_history()
    # The stored history is untouched (nothing deleted)…
    assert ("user", "first") in [(m["role"], m["content"]) for m in b._chat_history]
    # …but the model view no longer contains the excluded turn.
    assert ("user", "first") not in [(m["role"], m["content"]) for m in visible]
    assert ("user", "second") in [(m["role"], m["content"]) for m in visible]
    assert visible[0]["role"] == "user"  # still opens on a user message

    # Re-include restores it (reversible — nothing was lost).
    assert b.set_turn_included("c1", included=True)["included"] is True
    assert ("user", "first") in [
        (m["role"], m["content"]) for m in b._visible_history()
    ]


def test_excluded_turn_not_sent_to_the_model():
    """The excluded turn must actually be absent from the messages handed to the
    adapter on the next turn (not just from _visible_history in isolation)."""
    b, ad = _StubBridge(), _TextAdapter("ok")
    asyncio.run(b._agentic_chat_turn("c1", "secret tangent", ad))
    b.set_turn_included("c1", included=False)
    asyncio.run(b._agentic_chat_turn("c2", "next", ad))
    sent = [(m["role"], m["content"]) for m in ad.seen[-1]]
    assert ("user", "secret tangent") not in sent
    assert ("user", "next") in sent


def test_exclude_keeps_tool_pairs_intact():
    """Excluding a plain turn adjacent to a tool-calling turn must not orphan a
    tool_result in the visible history."""
    b = _StubBridge()
    b._chat_history = [
        {"role": "user", "content": "q0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]},
        {"role": "assistant", "content": "a1"},
    ]
    b._history_turn_ids = ["c0", "c0", "c1", "c1", "c1", "c1"]
    b._excluded_turns = {"c0"}
    visible = b._visible_history()
    # c0 gone; the tool pair (whole c1 turn) survives and opens on a user string.
    assert visible[0] == {"role": "user", "content": "q1"}
    assert any(
        isinstance(m.get("content"), list)
        and m["content"][0].get("type") == "tool_result"
        for m in visible
    )


def test_trim_keeps_tags_aligned():
    """_trim_chat_history must slice the aligned turn-id tags in lockstep, so a
    later exclude still maps tags to the right messages."""
    b = _StubBridge()
    b._chat_history = _long_history(40)  # 80 messages
    b._history_turn_ids = [f"c{i // 2}" for i in range(80)]
    b._trim_chat_history(max_messages=10)
    assert len(b._history_turn_ids) == len(b._chat_history)
    # Tags line up with the messages that survived (the most recent turns).
    assert b._history_turn_ids[-1] == "c39"


def test_compact_drops_excluded_turns_without_summarizing(monkeypatch):
    """Compaction bakes in the exclude decision: an excluded turn is physically
    dropped and must NOT appear in the folded summary note (which would re-inject
    the removed content), and its chat_id is reported so the card is retired."""
    import runspec_console.config as cfg

    monkeypatch.setattr(cfg, "read_chat_history", lambda: {"resume_keep_recent": 4})
    b = _StubBridge()
    b._chat_history = _long_history(10)  # 20 messages, q0..q9
    b._history_turn_ids = [f"c{i // 2}" for i in range(20)]
    b._excluded_turns = {"c0"}  # exclude the earliest turn (q0/a0)
    b.compact_chat()

    # The excluded turn's content is gone from history AND absent from the note.
    joined = "".join(
        m["content"] for m in b._chat_history if isinstance(m["content"], str)
    )
    assert "q0" not in joined
    # Tags stayed aligned through both the exclude-drop and the prefix compaction.
    assert len(b._history_turn_ids) == len(b._chat_history)
    # The compaction event reports the retired turn(s) for the frontend.
    events = [d for e, d in b.dispatched if e == "runspec:chat_compacted"]
    assert events and "c0" in events[-1]["dropped_turns"]


def test_set_turn_included_rejects_empty_id():
    b = _StubBridge()
    assert b.set_turn_included("", included=False) == {"ok": False, "included": True}
    assert b._excluded_turns == set()


def test_new_turn_after_resume_is_excludable():
    """In a resumed session the old (rebuilt) history carries the neutral ""
    sentinel tag (never excludable), but a NEW turn sent after resume is tagged
    with its own chat_id and excludes normally — the resumed prefix stays."""
    b, ad = _StubBridge(), _TextAdapter("reply")
    # Mimic resume_conversation's rebuilt state: text history, sentinel tags.
    b._chat_history = [
        {"role": "user", "content": "old q"},
        {"role": "assistant", "content": "old a"},
    ]
    b._history_turn_ids = ["", ""]

    # A fresh turn after resume — tagged with its real chat_id.
    asyncio.run(b._agentic_chat_turn("c-new", "new question", ad))
    assert "c-new" in b._history_turn_ids

    # The new card excludes; the resumed prefix is untouched and stays visible.
    assert b.set_turn_included("c-new", included=False)["included"] is False
    visible = [(m["role"], m["content"]) for m in b._visible_history()]
    assert ("user", "new question") not in visible
    assert ("user", "old q") in visible  # sentinel-tagged history never drops

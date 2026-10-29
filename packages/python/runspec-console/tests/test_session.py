"""
test_session.py — the pure ChatSession model: identity rules, the history
helpers that moved off the Bridge (tags / visible / trim / drop), and the
finished-automation pruning the registry applies.
"""

from __future__ import annotations

from runspec_console.session import (
    history_limits,
    FINISHED_AUTOMATION_CAP,
    ChatSession,
    new_automation,
    new_interactive,
    prune_finished,
    session_label,
)


def _hist(n: int) -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        out.append({"role": "user", "content": f"q{i}"})
        out.append({"role": "assistant", "content": f"a{i}"})
    return out


# ── identity ──────────────────────────────────────────────────────────────────


def test_interactive_session_id_is_its_conversation_and_workspace():
    s = new_interactive("abc123")
    assert s.interactive and s.kind == "chat"
    assert s.id == s.conversation_id == s.workspace_id == "abc123"
    fresh = new_interactive()
    assert len(fresh.id) == 16 and fresh.conversation_id == fresh.id


def test_automation_session_id_is_the_card_id_with_prefixed_workspace():
    t = new_automation("trigger", "card-1", "nightly · new_mail")
    assert not t.interactive and t.conversation_id == ""  # never saved
    assert t.id == "card-1" and t.workspace_id == "trigger-card-1"
    s = new_automation("schedule", "card-2", "job")
    assert s.workspace_id == "schedule-card-2"
    # An unknown kind collapses to trigger (the sweep knows only two prefixes).
    assert new_automation("portal", "card-3", "x").kind == "trigger"


def test_to_dict_is_the_session_event_shape():
    s = new_interactive("id1", label="hello world")
    s.state, s.current_turn = "running", "c1"
    assert s.to_dict() == {
        "id": "id1",
        "kind": "chat",
        "label": "hello world",
        "interactive": True,
        "created_at": s.created_at,
        "state": "running",
        "turn": "c1",
        "turns": 0,
    }


def test_session_label_collapses_and_clips():
    assert session_label("  hello   there  ") == "hello there"
    # A multi-line prompt is labelled by its first non-blank line only.
    assert session_label("\n  hello\n  world  ") == "hello"
    long = "x" * 100
    assert session_label(long).endswith("…") and len(session_label(long)) == 60


# ── history helpers (moved off the Bridge) ────────────────────────────────────


def test_sync_tags_pads_and_truncates():
    s = ChatSession(id="s")
    s.history = _hist(2)
    s.sync_tags("c1")
    assert s.turn_ids == ["c1"] * 4
    s.history.append({"role": "user", "content": "more"})
    s.sync_tags("c2")
    assert s.turn_ids == ["c1"] * 4 + ["c2"]
    del s.history[1:]
    s.sync_tags("c3")
    assert s.turn_ids == ["c1"]  # truncated to the history length


def test_visible_filters_excluded_turns_and_never_returns_empty():
    s = ChatSession(id="s")
    s.history = _hist(2)
    s.turn_ids = ["c0", "c0", "c1", "c1"]
    assert s.visible() is s.history  # no exclusions → the list itself
    s.set_included("c0", False)
    assert [m["content"] for m in s.visible()] == ["q1", "a1"]
    s.set_included("c1", False)
    assert s.visible() == s.history  # everything excluded → fall back to all
    assert s.set_included("c0", True) is True


def test_visible_trims_leading_tool_result_after_exclusion():
    s = ChatSession(id="s")
    s.history = [
        {"role": "user", "content": "q0"},
        {"role": "assistant", "content": [{"type": "tool_use"}]},
        {"role": "user", "content": [{"type": "tool_result"}]},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    s.turn_ids = ["c0", "c0", "c0", "c0", "c1", "c1"]
    s.excluded = {"c0"}
    assert [m["content"] for m in s.visible()] == ["q1", "a1"]


def test_trim_mutates_in_place_and_keeps_tags_aligned():
    s = ChatSession(id="s")
    s.history = _hist(40)
    s.turn_ids = [f"c{i // 2}" for i in range(80)]
    hist_obj, tags_obj = s.history, s.turn_ids
    s.trim(max_messages=10, keep=6)
    assert s.history is hist_obj and s.turn_ids is tags_obj  # same objects
    assert len(s.history) == 6 == len(s.turn_ids)
    assert s.history[0]["role"] == "user"
    assert s.turn_ids[-1] == "c39"


def test_trim_cuts_to_half_so_the_start_stays_put():
    # Cutting to the cap would change the history's first message on every
    # turn (and lose the prompt cache); cutting to half leaves it alone until
    # the history grows back past the cap.
    s = ChatSession(id="s")
    s.history = _hist(31)  # 62 messages
    s.turn_ids = [f"c{i // 2}" for i in range(62)]
    s.trim(max_messages=60)
    assert len(s.history) == 30
    first = s.history[0]
    for i in range(31, 45):  # 14 more exchanges → 58 messages, under the cap
        s.history += [
            {"role": "user", "content": f"q{i}"},
            {"role": "assistant", "content": f"a{i}"},
        ]
        s.turn_ids += [f"c{i}", f"c{i}"]
        s.trim(max_messages=60)
        assert s.history[0] is first


def test_trim_folds_the_opening_message_into_the_new_start():
    s = ChatSession(id="s")
    s.history = _hist(40)
    s.turn_ids = [f"c{i // 2}" for i in range(80)]
    s.trim(max_messages=10, keep=4)
    assert s.opening == "q0"
    first = s.history[0]["content"]
    assert first.startswith("[Older turns of this chat were dropped")
    assert "\nq0\n" in first and first.endswith("q38")
    # A second trim folds the original opening again, never the folded text.
    for i in range(40, 50):
        s.history += [
            {"role": "user", "content": f"q{i}"},
            {"role": "assistant", "content": f"a{i}"},
        ]
        s.turn_ids += [f"c{i}", f"c{i}"]
    s.trim(max_messages=10, keep=4)
    first = s.history[0]["content"]
    assert (
        first.count("[Older turns") == 1 and "\nq0\n" in first and first.endswith("q48")
    )


def test_history_limits_from_config():
    assert history_limits({}) == (60, 30)
    assert history_limits({"llm": {"history_max_messages": 100}}) == (100, 50)
    assert history_limits(
        {"llm": {"history_max_messages": 100, "history_trim_to": 70}}
    ) == (100, 70)
    # keep must sit below the cap, else no gap and a cut every turn again
    assert history_limits(
        {"llm": {"history_max_messages": 40, "history_trim_to": 40}}
    ) == (40, 20)
    assert history_limits({"llm": {"history_max_messages": 1}}) == (4, 2)
    assert history_limits({"llm": {"history_max_messages": "x"}}) == (60, 30)


def test_trim_clips_a_long_opening():
    s = ChatSession(id="s")
    s.history = [{"role": "user", "content": "x" * 5000}] + _hist(10)[1:]
    s.turn_ids = [""] * len(s.history)
    s.trim(max_messages=6, keep=2)
    assert len(s.history[0]["content"]) < 2300


def test_trim_drops_orphaned_tool_result_prefix():
    s = ChatSession(id="s")
    s.history = [
        {"role": "user", "content": "q0"},
        {"role": "assistant", "content": [{"type": "tool_use"}]},
        {"role": "user", "content": [{"type": "tool_result"}]},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    s.turn_ids = ["c0"] * 4 + ["c1"] * 2
    s.trim(max_messages=4, keep=3)
    assert [m["role"] for m in s.history] == ["user", "assistant"]
    assert s.history[0]["content"].endswith("q1")
    assert s.history[1] == {"role": "assistant", "content": "a1"}
    assert s.turn_ids == ["c1", "c1"]


def test_drop_turns_removes_by_tag_in_place():
    s = ChatSession(id="s")
    s.history = _hist(3)
    s.turn_ids = ["c0", "c0", "c1", "c1", "c2", "c2"]
    hist_obj = s.history
    assert s.drop_turns({"c1"}) == 2
    assert s.history is hist_obj
    assert [m["content"] for m in s.history] == ["q0", "a0", "q2", "a2"]
    assert s.turn_ids == ["c0", "c0", "c2", "c2"]
    assert s.drop_turns(set()) == 0


def test_replace_history_keeps_list_identity():
    s = ChatSession(id="s")
    hist_obj = s.history
    s.replace_history(_hist(1))
    assert s.history is hist_obj and s.turn_ids == ["", ""]
    s.replace_history(_hist(2), ["a", "a", "b", "b"])
    assert s.turn_ids == ["a", "a", "b", "b"]


def test_owns_turn_by_current_or_tag():
    s = ChatSession(id="s")
    s.turn_ids = ["c1"]
    s.current_turn = "c2"
    assert s.owns_turn("c1") and s.owns_turn("c2") and not s.owns_turn("c3")


# ── registry pruning ──────────────────────────────────────────────────────────


def test_prune_finished_keeps_newest_cap_and_never_touches_interactive():
    sessions: dict[str, ChatSession] = {}
    chat = new_interactive("chat1")
    chat.state = "done"  # even a (nonsensical) finished chat is never evicted
    sessions[chat.id] = chat
    for i in range(FINISHED_AUTOMATION_CAP + 3):
        s = new_automation("trigger", f"t{i}", "x")
        s.created_at = float(i)
        s.state = "done"
        sessions[s.id] = s
    running = new_automation("schedule", "live", "x")
    running.state = "running"
    sessions[running.id] = running
    evicted = prune_finished(sessions)
    assert evicted == ["t0", "t1", "t2"]  # the oldest three
    assert "chat1" in sessions and "live" in sessions
    assert len(
        [s for s in sessions.values() if s.state == "done" and not s.interactive]
    ) == (FINISHED_AUTOMATION_CAP)

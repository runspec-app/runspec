"""
Tests for trigger_runner — the engine handler that drives card lifecycle and
calls into the bridge. Verified against a recording FakeOps (no LLM / COM).
"""

from __future__ import annotations

from typing import Any

from runspec_console.events import Event
from runspec_console.triggers import Column, Trigger, TriggerCard
from runspec_console.trigger_runner import (
    compose_system,
    draft_reply_schema,
    make_handler,
    render_event,
)


class FakeOps:
    def __init__(
        self, tool_output: str = "ok", agent_summary: str = "agent reply"
    ) -> None:
        self.agent_calls: list[dict[str, Any]] = []
        self.rule_calls: list[dict[str, Any]] = []
        self.chat_notes: list[str] = []
        self._tool_output = tool_output
        self._agent_summary = agent_summary

    def run_agent_turn(
        self, chat_id, system, message, *, draft, reply_to=None, record=None
    ):
        self.agent_calls.append(
            {
                "chat_id": chat_id,
                "system": system,
                "message": message,
                "draft": draft,
                "reply_to": reply_to,
                "record_callable": callable(record),
            }
        )
        return self._agent_summary

    def run_rule_tool(self, chat_id, tool_name, tool_input):
        self.rule_calls.append(
            {"chat_id": chat_id, "tool_name": tool_name, "tool_input": tool_input}
        )
        return self._tool_output

    def note_in_chat(self, text):
        self.chat_notes.append(text)


def _card(trigger_name="t"):
    moves: list[tuple[str, dict]] = []
    card = TriggerCard(
        id="card1",
        trigger=trigger_name,
        source="outlook",
        event_type="new_mail",
        summary="hi",
        _on_change=lambda c: moves.append((c.column, dict(c.detail))),
    )
    return card, moves


# ── render_event / compose_system ─────────────────────────────────────────────


def test_render_event_includes_fields_and_body_last():
    ev = Event.new(
        "outlook",
        "new_mail",
        {"from": "a@b.com", "subject": "Outage", "body": "it is down"},
    )
    text = render_event(ev)
    assert "'new_mail' event" in text
    assert "- from: a@b.com" in text
    assert "- subject: Outage" in text
    assert text.rstrip().endswith("it is down")  # body rendered last
    assert "- body:" not in text  # body not inlined as a field


def test_render_event_joins_list_fields():
    ev = Event.new("outlook", "new_mail", {"categories": ["Red", "Blue"]})
    assert "- categories: Red, Blue" in render_event(ev)


def test_compose_system_includes_rules():
    t = Trigger.from_config(
        {"name": "t", "source": "outlook", "event": "new_mail", "rules": "Be careful."}
    )
    sys = compose_system(t, "You are helpful.")
    assert "You are helpful." in sys
    assert "Rules for this trigger:" in sys and "Be careful." in sys


def test_compose_system_draft_note_only_when_available():
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "reply": {"draft": True, "style": "terse"},
        }
    )
    assert "draft_reply" not in compose_system(t, "base", draft_available=False)
    on = compose_system(t, "base", draft_available=True)
    assert "draft_reply" in on and "terse" in on


# ── handler: agent mode ───────────────────────────────────────────────────────


def test_handler_agent_mode():
    ops = FakeOps()
    t = Trigger.from_config(
        {"name": "t", "source": "outlook", "event": "new_mail", "rules": "Triage it."}
    )
    handler = make_handler(ops, base_system="BASE")
    card, moves = _card()
    ev = Event.new("outlook", "new_mail", {"subject": "Outage"})

    handler(card, t, ev)

    assert len(ops.agent_calls) == 1
    call = ops.agent_calls[0]
    assert call["chat_id"] == "card1"
    assert "BASE" in call["system"] and "Triage it." in call["system"]
    assert "Outage" in call["message"]
    assert [c for c, _ in moves] == [Column.TRIAGE, Column.DONE]
    assert moves[-1][1]["result"] == "agent reply"  # summary lands on the card
    assert call["record_callable"] is True  # timeline recorder passed through
    assert ops.chat_notes == []  # post_to_chat off by default


def test_handler_agent_post_to_chat():
    ops = FakeOps()
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "post_to_chat": True,
        }
    )
    handler = make_handler(ops)
    card, _ = _card()
    handler(card, t, Event.new("outlook", "new_mail", {"subject": "x"}))
    assert len(ops.chat_notes) == 1 and "Trigger 't'" in ops.chat_notes[0]


def test_handler_agent_draft_only_when_both_enabled():
    ops = FakeOps()
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "reply": {"draft": True},
        }
    )
    # draft_available False at the bridge → handler must not request a draft
    make_handler(ops)(*(_card()[0], t, Event.new("outlook", "new_mail", {})))
    assert ops.agent_calls[0]["draft"] is False

    ops2 = FakeOps()
    card, _ = _card()
    make_handler(ops2, draft_available=True)(
        card, t, Event.new("outlook", "new_mail", {"entry_id": "ENT9"})
    )
    assert ops2.agent_calls[0]["draft"] is True
    assert ops2.agent_calls[0]["reply_to"] == "ENT9"  # bound from the event


def test_handler_reply_to_none_without_entry_id():
    """draft enabled but no entry_id (e.g. a reminder) → no reply target."""
    ops = FakeOps()
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "reply": {"draft": True},
        }
    )
    card, _ = _card()
    make_handler(ops, draft_available=True)(
        card, t, Event.new("outlook", "new_mail", {})
    )
    assert ops.agent_calls[0]["reply_to"] is None


def test_draft_reply_schema_shape():
    schema = draft_reply_schema()
    assert schema["name"] == "draft_reply"
    assert schema["input_schema"]["required"] == ["body"]
    assert "never sends" in schema["description"]


# ── handler: rule mode ────────────────────────────────────────────────────────


def test_handler_rule_mode():
    ops = FakeOps(tool_output="exit 0\nall good")
    t = Trigger.from_config(
        {
            "name": "r",
            "source": "outlook",
            "event": "new_mail",
            "mode": "rule",
            "run": "host__check",
            "map": {"svc": "{{ subject }}"},
            "post_to_chat": True,
        }
    )
    handler = make_handler(ops)
    card, moves = _card("r")
    handler(card, t, Event.new("outlook", "new_mail", {"subject": "web01"}))

    assert ops.rule_calls == [
        {"chat_id": "card1", "tool_name": "host__check", "tool_input": {"svc": "web01"}}
    ]
    assert [c for c, _ in moves] == [Column.RUNNING, Column.DONE]
    assert moves[-1][1]["result"] == "exit 0\nall good"
    assert ops.agent_calls == []
    assert len(ops.chat_notes) == 1 and "host__check" in ops.chat_notes[0]

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
    mail_scope_note,
    make_handler,
    propose_reply_schema,
    render_event,
)


class FakeOps:
    def __init__(
        self,
        tool_output: str = "ok",
        agent_summary: str = "agent reply",
        mailbox: str = "",
        group_outputs: list[str] | None = None,
        pre_turn_result: dict[str, Any] | None = None,
    ) -> None:
        self.agent_calls: list[dict[str, Any]] = []
        self.rule_calls: list[dict[str, Any]] = []
        self.group_calls: list[dict[str, Any]] = []
        self.pre_turn_calls: list[dict[str, Any]] = []
        self.chat_mirrors: list[dict[str, str]] = []
        self.sound_calls: list[str] = []
        self._tool_output = tool_output
        self._agent_summary = agent_summary
        self._mailbox = mailbox  # what queue_mailbox_for returns for a queue event
        self._group_outputs = group_outputs
        self._pre_turn_result = pre_turn_result or {}

    def run_agent_turn(
        self,
        chat_id,
        system,
        message,
        *,
        draft,
        reply_to=None,
        room=None,
        from_mailbox=None,
        allowed_runnables=None,
        action_allow=None,
        record=None,
        stage=None,
        trigger="",
    ):
        self.agent_calls.append(
            {
                "chat_id": chat_id,
                "system": system,
                "message": message,
                "draft": draft,
                "reply_to": reply_to,
                "room": room,
                "from_mailbox": from_mailbox,
                "allowed_runnables": allowed_runnables,
                "action_allow": action_allow,
                "record_callable": callable(record),
                "trigger": trigger,
            }
        )
        return self._agent_summary

    def queue_mailbox_for(self, payload):
        # The bridge resolves a help-desk recipient to its shared mailbox; the
        # fake returns whatever this case was constructed with ("" = personal).
        return self._mailbox

    def run_rule_tool(self, chat_id, tool_name, tool_input, trigger=""):
        self.rule_calls.append(
            {
                "chat_id": chat_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "trigger": trigger,
            }
        )
        return self._tool_output

    def run_rule_group(self, chat_id, calls, trigger=""):
        self.group_calls.append(
            {"chat_id": chat_id, "calls": list(calls), "trigger": trigger}
        )
        if self._group_outputs is not None:
            return self._group_outputs
        return [self._tool_output for _ in calls]

    def expand_targets(self, run):
        # Default: identity (plain host__runnable). Tests that exercise group
        # fan-out set self._expand to a custom map.
        if getattr(self, "_expand", None) is not None:
            return list(self._expand.get(run, [run]))
        return [run]

    def run_pre_turn(self, chat_id, instructions, event_text):
        self.pre_turn_calls.append(
            {
                "chat_id": chat_id,
                "instructions": instructions,
                "event_text": event_text,
            }
        )
        return dict(self._pre_turn_result)

    def mirror_to_chat(self, kind, label, text):
        self.chat_mirrors.append({"kind": kind, "label": label, "text": text})

    def play_sound(self, identifier):
        self.sound_calls.append(identifier)


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


def test_render_event_dedupes_text_equal_body():
    # The paste source mirrors its input into both `text` and `body`; agent mode
    # should render it once (under "Body:"), not also inline as `- text: …`.
    ev = Event.new("paste", "text", {"text": "paste me", "body": "paste me"})
    text = render_event(ev)
    assert "- text:" not in text
    assert text.rstrip().endswith("paste me")
    assert text.count("paste me") == 1


def test_render_event_keeps_text_when_it_differs_from_body():
    # Only the exact duplicate is suppressed — a distinct `text` still renders.
    ev = Event.new("paste", "text", {"text": "a note", "body": "the body"})
    text = render_event(ev)
    assert "- text: a note" in text
    assert text.rstrip().endswith("the body")


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


def test_mail_scope_note_names_mailbox_and_folder():
    ev = Event.new(
        "outlook",
        "new_mail",
        {"mailbox": "helpdesk@corp.com", "folder": "Tickets/Open"},
    )
    note = mail_scope_note(ev)
    assert "helpdesk@corp.com" in note and "Tickets/Open" in note
    assert "mailbox='helpdesk@corp.com'" in note and "folder='Tickets/Open'" in note
    assert "outlook_search" in note


def test_mail_scope_note_empty_without_location_or_for_other_events():
    # A synthetic "Trigger now" event with no mailbox/folder → no note.
    assert mail_scope_note(Event.new("outlook", "new_mail", {"body": "hi"})) == ""
    # Non-mail events never get the note.
    assert mail_scope_note(Event.new("outlook", "reminder", {"folder": "x"})) == ""
    assert mail_scope_note(Event.new("chat", "message", {"folder": "x"})) == ""


def test_mail_scope_note_folder_only():
    ev = Event.new("outlook", "new_mail", {"folder": "Alerts"})
    note = mail_scope_note(ev)
    assert "folder 'Alerts'" in note and "folder='Alerts'" in note
    assert "mailbox=" not in note  # default account → no mailbox arg


def test_compose_system_folds_in_mail_scope():
    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    ev = Event.new("outlook", "new_mail", {"mailbox": "hd@corp.com", "folder": "Open"})
    sys = compose_system(t, "base", event=ev)
    assert "folder='Open'" in sys and "mailbox='hd@corp.com'" in sys
    # No event, or a located event omitted → no scope directive.
    assert "folder=" not in compose_system(t, "base")


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
    assert ops.chat_mirrors == []  # post_to_chat off by default


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
    assert len(ops.chat_mirrors) == 1
    mirror = ops.chat_mirrors[0]
    assert mirror["kind"] == "chat"  # agent mode → chat card
    assert "t" in mirror["label"] and mirror["text"] == "agent reply"


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


def test_handler_email_draft_binds_from_mailbox():
    """A help-desk email draft replies as the shared mailbox (the queue identity):
    from_mailbox is bound from the bridge's queue resolution."""
    ops = FakeOps(mailbox="it-support@corp.com")
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
        card, t, Event.new("outlook", "new_mail", {"entry_id": "ENT9"})
    )
    assert ops.agent_calls[0]["from_mailbox"] == "it-support@corp.com"


def test_handler_email_claimed_event_drafts_as_mail():
    """A draft-on-claim trigger (source='email', event='claimed') routes like any
    mail draft: reply_to from the event's entry_id, send-as the queue mailbox."""
    ops = FakeOps(mailbox="it-support@corp.com")
    t = Trigger.from_config(
        {
            "name": "draft-on-claim",
            "source": "email",
            "event": "claimed",
            "reply": {"draft": True},
        }
    )
    card, _ = _card()
    make_handler(ops, draft_available=True)(
        card,
        t,
        Event.new("email", "claimed", {"entry_id": "ENT1", "body": "help"}),
    )
    call = ops.agent_calls[0]
    assert call["draft"] is True
    assert call["reply_to"] == "ENT1"  # the requester message to reply to
    assert call["from_mailbox"] == "it-support@corp.com"
    assert call["room"] is None  # not a chat draft


def test_handler_personal_mail_draft_has_no_from_mailbox():
    """Non-queue mail drafts from the operator: from_mailbox stays None."""
    ops = FakeOps(mailbox="")  # queue_mailbox_for → "" (not a mapped mailbox)
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
        card, t, Event.new("outlook", "new_mail", {"entry_id": "ENT9"})
    )
    assert ops.agent_calls[0]["from_mailbox"] is None


def test_handler_chat_draft_has_no_from_mailbox():
    """A chat draft (propose_reply) never resolves a mailbox."""
    ops = FakeOps(mailbox="it-support@corp.com")
    t = Trigger.from_config(
        {"name": "t", "source": "chat", "event": "message", "reply": {"draft": True}}
    )
    card, _ = _card()
    make_handler(ops, draft_available=True)(
        card, t, Event.new("chat", "message", {"room": "ops", "text": "hi"})
    )
    assert ops.agent_calls[0]["from_mailbox"] is None


def test_handler_binds_runnables_allowlist():
    """An agent trigger with a runnables allowlist scopes the turn's tools."""
    ops = FakeOps()
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "runnables": ["web01__check-memory", "restart-service"],
        }
    )
    card, _ = _card()
    make_handler(ops)(card, t, Event.new("outlook", "new_mail", {}))
    assert ops.agent_calls[0]["allowed_runnables"] == [
        "web01__check-memory",
        "restart-service",
    ]


def test_handler_no_allowlist_passes_none():
    """No runnables key → allowed_runnables is None (the agent sees every tool)."""
    ops = FakeOps()
    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    card, _ = _card()
    make_handler(ops)(card, t, Event.new("outlook", "new_mail", {}))
    assert ops.agent_calls[0]["allowed_runnables"] is None
    assert ops.agent_calls[0]["action_allow"] is None  # no action allowlist either


def test_handler_passes_action_allow():
    """An agent trigger's action_allow scopes the built-in action/Outlook tools."""
    ops = FakeOps()
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "action_allow": ["create_document", "outlook_draft"],
        }
    )
    card, _ = _card()
    make_handler(ops)(card, t, Event.new("outlook", "new_mail", {}))
    assert ops.agent_calls[0]["action_allow"] == ["create_document", "outlook_draft"]


def test_handler_plays_trigger_sound_agent_mode():
    """An agent-mode trigger with a `sound` plays it once as the turn begins."""
    ops = FakeOps()
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "sound": "alarm",
        }
    )
    make_handler(ops)(*(_card()[0], t, Event.new("outlook", "new_mail", {})))
    assert ops.sound_calls == ["alarm"]
    assert len(ops.agent_calls) == 1  # the action still ran


def test_handler_plays_trigger_sound_rule_mode():
    """A rule-mode trigger's `sound` fires before the runnable; the run still runs."""
    ops = FakeOps()
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "outlook",
            "event": "new_mail",
            "mode": "rule",
            "run": "web01__restart",
            "sound": "sound:deploy-done",
        }
    )
    make_handler(ops)(*(_card()[0], t, Event.new("outlook", "new_mail", {})))
    assert ops.sound_calls == ["sound:deploy-done"]
    assert ops.rule_calls[0]["tool_name"] == "web01__restart"


def test_handler_no_sound_never_calls_play():
    """A trigger without a `sound` never touches the audio path."""
    ops = FakeOps()
    t = Trigger.from_config({"name": "t", "source": "outlook", "event": "new_mail"})
    make_handler(ops)(*(_card()[0], t, Event.new("outlook", "new_mail", {})))
    assert ops.sound_calls == []


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


# ── handler: chat-room drafting (propose_reply, not draft_reply) ───────────────


def test_compose_system_room_draft_note():
    """draft_kind='room' describes propose_reply (composer), not draft_reply."""
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "chat",
            "event": "message",
            "reply": {"draft": True, "style": "friendly"},
        }
    )
    sys = compose_system(t, "base", draft_available=True, draft_kind="room")
    assert "propose_reply" in sys and "composer" in sys
    assert "draft_reply" not in sys
    assert "friendly" in sys


def test_handler_chat_draft_binds_room_not_reply_to():
    """A chat-message event drafts to the room composer: room bound from the
    payload, reply_to left None (no entry_id), system mentions propose_reply."""
    ops = FakeOps()
    t = Trigger.from_config(
        {
            "name": "t",
            "source": "chat",
            "event": "message",
            "reply": {"draft": True},
        }
    )
    card, _ = _card("t")
    make_handler(ops, draft_available=True)(
        card, t, Event.new("chat", "message", {"room": "ops", "text": "help"})
    )
    call = ops.agent_calls[0]
    assert call["draft"] is True
    assert call["room"] == "ops"  # bound from the event
    assert call["reply_to"] is None  # chat has no entry_id
    assert "propose_reply" in call["system"]


def test_propose_reply_schema_shape():
    schema = propose_reply_schema()
    assert schema["name"] == "propose_reply"
    assert schema["input_schema"]["required"] == ["text"]
    assert "NEVER sent" in schema["description"]


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
        {
            "chat_id": "card1",
            "tool_name": "host__check",
            "tool_input": {"svc": "web01"},
            "trigger": "outlook",  # provenance = the causing event source
        }
    ]
    assert [c for c, _ in moves] == [Column.RUNNING, Column.DONE]
    assert moves[-1][1]["result"] == "exit 0\nall good"
    assert ops.agent_calls == []
    assert len(ops.chat_mirrors) == 1
    mirror = ops.chat_mirrors[0]
    assert mirror["kind"] == "run"  # rule mode → slash-command run card
    # label mirrors "/runnable on host" from run="host__check"
    assert mirror["label"].startswith("/check on host")
    assert mirror["text"] == "exit 0\nall good"


# ── handler: declarative multi-runnable actions (Phase 2) ──────────────────────


def test_handler_rule_actions_sequential_runs_each_in_order():
    ops = FakeOps(tool_output="done")
    t = Trigger.from_config(
        {
            "name": "seq",
            "source": "schedule",
            "event": "nightly",
            "mode": "rule",
            "action_mode": "sequential",
            "actions": [
                {"run": "web01__check-memory", "map": {"svc": "{{ subject }}"}},
                {"run": "db01__check-disk"},
            ],
        }
    )
    card, moves = _card("seq")
    make_handler(ops)(card, t, Event.new("schedule", "nightly", {"subject": "web"}))

    # Each step ran through the single-tool gate, in order, with rendered args.
    assert [c["tool_name"] for c in ops.rule_calls] == [
        "web01__check-memory",
        "db01__check-disk",
    ]
    assert ops.rule_calls[0]["tool_input"] == {"svc": "web"}
    assert ops.group_calls == []  # sequential never uses the group path
    assert [c for c, _ in moves] == [Column.RUNNING, Column.DONE]


def test_handler_rule_actions_parallel_uses_group():
    ops = FakeOps(group_outputs=["a-out", "b-out"])
    t = Trigger.from_config(
        {
            "name": "par",
            "source": "schedule",
            "event": "nightly",
            "mode": "rule",
            "action_mode": "parallel",
            "actions": [
                {"run": "web01__check-memory"},
                {"run": "db01__check-disk"},
            ],
        }
    )
    card, moves = _card("par")
    make_handler(ops)(card, t, Event.new("schedule", "nightly", {}))

    # Parallel routes through the single grouped-confirm path, not per-tool gates.
    assert ops.rule_calls == []
    assert len(ops.group_calls) == 1
    calls = ops.group_calls[0]["calls"]
    assert [name for name, _ in calls] == ["web01__check-memory", "db01__check-disk"]
    result = moves[-1][1]["result"]
    assert "a-out" in result and "b-out" in result


# ── handler: host-group fan-out targets ───────────────────────────────────────


def test_handler_rule_run_group_fans_out_via_grouped_confirm():
    ops = FakeOps(group_outputs=["m1-out", "m2-out"])
    ops._expand = {"group:Prod__restart": ["web01__restart", "web02__restart"]}
    t = Trigger.from_config(
        {
            "name": "g",
            "source": "schedule",
            "event": "nightly",
            "mode": "rule",
            "run": "group:Prod__restart",
            "post_to_chat": True,
        }
    )
    card, moves = _card("g")
    make_handler(ops)(card, t, Event.new("schedule", "nightly", {}))

    # A single grouped confirm covers both members; no per-tool gate.
    assert ops.rule_calls == []
    assert len(ops.group_calls) == 1
    assert [n for n, _ in ops.group_calls[0]["calls"]] == [
        "web01__restart",
        "web02__restart",
    ]
    result = moves[-1][1]["result"]
    assert "── web01 ──\nm1-out" in result and "── web02 ──\nm2-out" in result
    # Mirrored as a group run card.
    assert ops.chat_mirrors[0]["label"].startswith("/restart on group:Prod")


def test_handler_rule_run_group_empty_reports_no_member():
    ops = FakeOps()
    ops._expand = {"group:Prod__restart": []}  # no member carries the runnable
    t = Trigger.from_config(
        {
            "name": "g",
            "source": "schedule",
            "event": "nightly",
            "mode": "rule",
            "run": "group:Prod__restart",
        }
    )
    card, moves = _card("g")
    make_handler(ops)(card, t, Event.new("schedule", "nightly", {}))
    assert ops.group_calls == []
    assert "No host in group 'Prod'" in moves[-1][1]["result"]


def test_handler_rule_actions_expand_group_step():
    ops = FakeOps(group_outputs=["a", "b", "c"])
    ops._expand = {"group:Prod__check": ["web01__check", "web02__check"]}
    t = Trigger.from_config(
        {
            "name": "par",
            "source": "schedule",
            "event": "nightly",
            "mode": "rule",
            "action_mode": "parallel",
            "actions": [
                {"run": "group:Prod__check"},  # expands to 2
                {"run": "db01__check-disk"},  # plain host
            ],
        }
    )
    card, _ = _card("par")
    make_handler(ops)(card, t, Event.new("schedule", "nightly", {}))
    assert [n for n, _ in ops.group_calls[0]["calls"]] == [
        "web01__check",
        "web02__check",
        "db01__check-disk",
    ]


# ── handler: optional LLM pre-turn (Phase 3) ───────────────────────────────────


def test_handler_pre_turn_feeds_action_args():
    ops = FakeOps(pre_turn_result={"host": "web42"})
    t = Trigger.from_config(
        {
            "name": "pre",
            "source": "schedule",
            "event": "alert",
            "mode": "rule",
            "pre_turn": "Find the affected host.",
            "run": "restart-service",
            "map": {"target": "{{ _llm.host }}"},
        }
    )
    card, _ = _card("pre")
    make_handler(ops)(card, t, Event.new("schedule", "alert", {"text": "x"}))

    assert len(ops.pre_turn_calls) == 1
    assert ops.pre_turn_calls[0]["instructions"] == "Find the affected host."
    # The pre-turn output landed under {{ _llm.* }} and rendered into the args.
    assert ops.rule_calls[0]["tool_input"] == {"target": "web42"}


def test_handler_no_pre_turn_skips_extraction():
    ops = FakeOps()
    t = Trigger.from_config(
        {
            "name": "nopre",
            "source": "schedule",
            "event": "alert",
            "mode": "rule",
            "run": "restart-service",
        }
    )
    card, _ = _card("nopre")
    make_handler(ops)(card, t, Event.new("schedule", "alert", {}))
    assert ops.pre_turn_calls == []

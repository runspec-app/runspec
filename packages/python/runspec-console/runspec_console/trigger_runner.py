"""
trigger_runner.py — turn a matched trigger into an action through the bridge.

The :class:`~runspec_console.triggers.TriggerEngine` calls a ``handler(card,
trigger, event)`` for each match. This module builds that handler. It is kept
separate from (and ignorant of) ``bridge.py`` so the routing-to-action logic is
unit-testable against a tiny ``BridgeOps`` protocol — no LLM, no COM, no
pywebview. The bridge supplies the real ``BridgeOps`` (its agent loop, autonomy
gate, and chat dispatch) in step 4b.

Card lifecycle this handler drives (the engine guarantees a terminal column if
we don't): rule mode → RUNNING → DONE; agent mode → TRIAGE → DONE. While an
agent turn is parked at the autonomy gate, the gate emits a card-scoped
``runspec:tool_confirm`` the UI overlays as the "awaiting human" column.

See docs/design/console-triggers.md (§5–§7, §11).
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from .events import Event
from .triggers import Column, Handler, Trigger, TriggerCard

# Cap the result text we attach to a card / mirror into chat.
_RESULT_CAP = 2000


class BridgeOps(Protocol):
    """The slice of the bridge a trigger handler needs."""

    def run_agent_turn(
        self,
        chat_id: str,
        system: str,
        message: str,
        *,
        draft: bool,
        reply_to: str | None = None,
        record: Callable[[str], None] | None = None,
        stage: Callable[[str], None] | None = None,
    ) -> str:
        """Run an isolated, headless agent turn under ``chat_id`` (the card id),
        with ``system`` as its system prompt and ``message`` as the sole user
        turn. ``draft`` requests the draft_reply tool be offered; ``reply_to`` is
        the source message's id the draft should reply to (required for drafting).
        ``record`` (if given) is called with a short note per tool/gate step, for
        the card timeline. ``stage`` reports the card stage ("awaiting_human" /
        "running") as the turn progresses. Returns a short summary for the card.
        """
        ...

    def run_rule_tool(
        self, chat_id: str, tool_name: str, tool_input: dict[str, Any]
    ) -> str:
        """Run a single runnable through the autonomy gate, returning its output
        (or a gate-decline message). ``chat_id`` is the card id."""
        ...

    def mirror_to_chat(self, kind: str, label: str, text: str) -> None:
        """Mirror a finished ``post_to_chat`` trigger run into the Console as the
        same kind of block a manual invocation makes: ``kind="run"`` (a
        slash-command run card, for mode=rule) or ``kind="chat"`` (an agent/chat
        card, for mode=agent). ``label`` is the card heading; ``text`` is the
        runnable output (run) or the agent's reply (chat)."""
        ...


def render_event(event: Event) -> str:
    """A readable rendering of an event for the agent's first user turn."""
    lines = [
        f"A '{event.type}' event arrived from the '{event.source}' source. "
        "Follow your rules and use the available tools as needed.",
        "",
        "Event details:",
    ]
    body: Any = None
    for key, value in event.payload.items():
        if key == "body":
            body = value
            continue
        if isinstance(value, list):
            value = ", ".join(str(x) for x in value)
        lines.append(f"- {key}: {value}")
    if body:
        lines += ["", "Body:", str(body)]
    return "\n".join(lines)


def compose_system(
    trigger: Trigger, base_system: str = "", *, draft_available: bool = False
) -> str:
    """Build the system prompt for an agent-mode trigger: the base assistant
    prompt, the trigger's own rules, and (when enabled) the draft-reply note."""
    parts: list[str] = []
    if base_system.strip():
        parts.append(base_system.strip())
    if trigger.rules.strip():
        parts.append("Rules for this trigger:\n" + trigger.rules.strip())
    if draft_available:
        note = (
            "You may draft a reply with the draft_reply tool. Save it as a draft "
            "only — never send. Keep it short; the user will review and send it."
        )
        if trigger.reply.style.strip():
            note += "\nPreferred style: " + trigger.reply.style.strip()
        parts.append(note)
    return "\n\n".join(parts)


def draft_reply_schema() -> dict[str, Any]:
    """Anthropic-format tool schema for the built-in draft_reply tool. Offered to
    an agent-mode trigger turn only when drafting is enabled and the event has a
    message to reply to. The reply is saved to Drafts — never sent."""
    return {
        "name": "draft_reply",
        "description": (
            "Draft a reply to the message that triggered this run and save it to "
            "the Drafts folder for the user to review and send. This never sends "
            "mail. Provide the reply body text only; the recipients and quoted "
            "original are filled in automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": "The reply body to save as a draft.",
                }
            },
            "required": ["body"],
        },
    }


def make_handler(
    ops: BridgeOps, *, base_system: str = "", draft_available: bool = False
) -> Handler:
    """Build the engine handler that runs each matched trigger via ``ops``."""

    def handler(card: TriggerCard, trigger: Trigger, event: Event) -> None:
        if trigger.mode == "rule":
            tool_input = trigger.render_args(event.payload)
            card.move(Column.RUNNING, tool=trigger.run, args=tool_input)
            output = ops.run_rule_tool(card.id, trigger.run, tool_input)
            card.move(Column.DONE, result=output[:_RESULT_CAP])
            if trigger.post_to_chat:
                # Mirror as a standard slash-command run card: "/runnable on host".
                host, _, name = trigger.run.partition("__")
                label = f"/{name} on {host}" if name else f"/{trigger.run}"
                ops.mirror_to_chat("run", f"{label}  (trigger: {trigger.name})", output)
            return

        draft = draft_available and trigger.reply.draft
        # Drafting needs a source message to reply to (Outlook mail carries an
        # entry_id); without one the bridge simply won't offer the draft tool.
        reply_to = (event.payload.get("entry_id") or None) if draft else None
        system = compose_system(trigger, base_system, draft_available=draft)
        card.move(Column.TRIAGE)
        summary = ops.run_agent_turn(
            card.id,
            system,
            render_event(event),
            draft=draft,
            reply_to=reply_to,
            record=card.log,  # per-tool/gate steps onto the card timeline
            stage=lambda col: card.move(col),  # awaiting_human → running stages
        )
        # The agent's reply + any actions land on the card — a trigger turn has no
        # chat transcript, so this is where its output is visible.
        card.move(Column.DONE, result=summary) if summary else card.move(Column.DONE)
        if trigger.post_to_chat:
            # Mirror as an agent/chat card — the same shape as an LLM turn.
            label = f"Trigger '{trigger.name}' · {event.type}"
            ops.mirror_to_chat("chat", label, summary or "(handled — no textual reply)")

    return handler

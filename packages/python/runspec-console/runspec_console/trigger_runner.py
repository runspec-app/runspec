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
from .triggers import Column, Handler, Trigger, TriggerCard, parse_target

# Cap the result text we attach to a card / mirror into chat.
_RESULT_CAP = 2000

# A declarative action list may fan groups out into many concrete calls. The list
# itself is capped at _MAX_ACTIONS (10) declared steps in triggers.py; this bounds
# the post-expansion total so a few group steps can't turn into a huge matrix —
# it stays a flat fire-and-collect batch, not a workflow.
_MAX_FANOUT_CALLS = 100


def _fanout_block(targets: list[str], outputs: list[str]) -> str:
    """Join per-host fan-out outputs with a host header per section."""
    return "\n\n".join(
        f"── {t.split('__', 1)[0]} ──\n{out}" for t, out in zip(targets, outputs)
    )


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
        room: str | None = None,
        from_mailbox: str | None = None,
        allowed_runnables: list[str] | None = None,
        action_allow: list[str] | None = None,
        record: Callable[[str], None] | None = None,
        stage: Callable[[str], None] | None = None,
        trigger: str = "",
        model: str = "",
    ) -> str:
        """Run an isolated, headless agent turn under ``chat_id`` (the card id),
        with ``system`` as its system prompt and ``message`` as the sole user
        turn. ``draft`` requests a reply-drafting tool be offered; for an Outlook
        event ``reply_to`` is the source message's id the draft replies to, and
        for a chat event ``room`` is the room a proposed reply targets (each is
        required to offer its respective tool). ``from_mailbox`` (Outlook only) is
        the shared-mailbox address to draft the reply *as* — the help-desk queue
        identity; empty/None drafts from the operator. ``allowed_runnables``, when
        non-empty, scopes the runnable tools the agent may call to that allowlist
        (None/empty = every runnable). ``record`` (if given) is called with a short
        note per tool/gate step, for the card timeline. ``stage`` reports the card
        stage ("awaiting_human" / "running") as the turn progresses. Returns a
        short summary for the card.
        """
        ...

    def queue_mailbox_for(self, payload: dict[str, Any]) -> str:
        """The shared-mailbox address to reply *as* for a help-desk email event
        (the queue identity), or ``""`` for personal/other mail (the operator
        drafts from their own identity) and any non-email event. Lets the handler
        bind the send-as mailbox without knowing the ``[mailboxes]`` map."""
        ...

    def run_rule_tool(
        self,
        chat_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        trigger: str = "",
    ) -> str:
        """Run a single runnable through the autonomy gate, returning its output
        (or a gate-decline message). ``chat_id`` is the card id; ``trigger`` is the
        causing event source, recorded as the run's provenance."""
        ...

    def run_rule_group(
        self,
        chat_id: str,
        calls: list[tuple[str, dict[str, Any]]],
        trigger: str = "",
    ) -> list[str]:
        """Run several rule-mode runnables as one parallel group behind a SINGLE
        confirm (most-restrictive autonomy across the steps; a ``manual`` step
        refuses the whole group). Returns one output string per call, in order."""
        ...

    def expand_targets(self, run: str) -> list[str]:
        """Expand a rule/action ``run`` target to concrete ``host__runnable`` tool
        names. A plain ``host__runnable`` returns itself; a
        ``group:<group>__<runnable>`` target expands to one entry per group member
        that carries the runnable (empty when none do)."""
        ...

    def run_pre_turn(
        self, chat_id: str, instructions: str, event_text: str, model: str = ""
    ) -> dict[str, Any]:
        """One headless LLM extract step: interpret the event per ``instructions``
        and return a flat ``{param: value}`` dict (exposed to the action maps under
        ``{{ _llm.* }}``). It has no runnable tools and cannot loop or branch — a
        pure extract. Returns ``{}`` on any failure (the action still runs)."""
        ...

    def mirror_to_chat(
        self, kind: str, label: str, text: str, card_id: str | None = None
    ) -> None:
        """Mirror a finished ``post_to_chat`` trigger run into the Console as the
        same kind of block a manual invocation makes: ``kind="run"`` (a
        slash-command run card, for mode=rule) or ``kind="chat"`` (an agent/chat
        card, for mode=agent). ``label`` is the card heading; ``text`` is the
        runnable output (run) or the agent's reply (chat)."""
        ...

    def play_sound(self, identifier: str) -> None:
        """Best-effort audio cue for a fired trigger — ``identifier`` is a named
        sound id / bundled cue name / token. No-op off Windows or when muted; never
        affects whether the action runs."""
        ...


def render_event(event: Event) -> str:
    """A readable rendering of an event for the agent's first user turn."""
    lines = [
        f"A '{event.type}' event arrived from the '{event.source}' source. "
        "Follow your rules and use the available tools as needed.",
        "",
        "Event details:",
    ]
    body: Any = event.payload.get("body")
    for key, value in event.payload.items():
        if key == "body":
            continue
        # The paste source mirrors its text into both `text` and `body`; don't
        # print it twice (it already renders under the "Body:" block below).
        if key == "text" and value == body:
            continue
        if isinstance(value, list):
            value = ", ".join(str(x) for x in value)
        lines.append(f"- {key}: {value}")
    if body:
        lines += ["", "Body:", str(body)]
    return "\n".join(lines)


def mail_scope_note(event: Event) -> str:
    """A directive telling the agent where the triggering email lives, so Outlook
    reads target that mailbox+folder rather than the default Inbox.

    An ``outlook``/``new_mail`` event is tagged with the ``mailbox`` and ``folder``
    it arrived in (see sources/outlook.py). Without this the agent, asked to find an
    earlier or similar message, searches the default Inbox and finds nothing (it
    even narrates "yesterday's inbox"). Returns ``""`` for non-mail events or when
    neither tag is present (e.g. a synthetic "Trigger now" event with no location),
    so nothing is added spuriously. Pure/testable."""
    if event.source != "outlook" or event.type != "new_mail":
        return ""
    mailbox = str(event.payload.get("mailbox") or "").strip()
    folder = str(event.payload.get("folder") or "").strip()
    if not mailbox and not folder:
        return ""
    where = " and ".join(
        p
        for p in (
            f"mailbox '{mailbox}'" if mailbox else "",
            f"folder '{folder}'" if folder else "",
        )
        if p
    )
    args = ", ".join(
        p
        for p in (
            f"mailbox='{mailbox}'" if mailbox else "",
            f"folder='{folder}'" if folder else "",
        )
        if p
    )
    return (
        f"This email arrived in {where}. When you search or read Outlook for "
        "related messages (an earlier, similar, or replying mail), target that "
        f"same location — pass {args} to the outlook_inbox / outlook_search / "
        "outlook_get_message tools. Do not search only the default Inbox."
    )


def compose_system(
    trigger: Trigger,
    base_system: str = "",
    *,
    draft_available: bool = False,
    draft_kind: str = "mail",
    event: Event | None = None,
) -> str:
    """Build the system prompt for an agent-mode trigger: the base assistant
    prompt, the trigger's own rules, an Outlook mailbox/folder scoping directive
    (when ``event`` is a located ``new_mail``), and (when enabled) the
    reply-drafting note.

    ``draft_kind`` selects which drafting tool the note describes — ``"mail"``
    for the Outlook ``draft_reply`` (saved to Drafts) or ``"room"`` for the chat
    ``propose_reply`` (pre-fills the room composer). Either way nothing is sent:
    the user reviews and sends."""
    parts: list[str] = []
    if base_system.strip():
        parts.append(base_system.strip())
    if trigger.rules.strip():
        parts.append("Rules for this trigger:\n" + trigger.rules.strip())
    if event is not None:
        scope = mail_scope_note(event)
        if scope:
            parts.append(scope)
    if draft_available:
        if draft_kind == "room":
            note = (
                "You may propose a reply with the propose_reply tool. It never "
                "sends — your text only lands in the room's composer for the user "
                "to review, edit, and send. Keep it short."
            )
        else:
            note = (
                "You may draft a reply with the draft_reply tool. Save it as a "
                "draft only — never send. Keep it short; the user will review and "
                "send it."
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


def propose_reply_schema() -> dict[str, Any]:
    """Anthropic-format tool schema for the built-in propose_reply tool — the
    chat-room analogue of draft_reply. Offered to an agent-mode trigger turn only
    when drafting is enabled and the event is a chat message (carries a ``room``).
    The text is placed in the room composer for the user to send — never sent by
    the agent (design §2: speak = draft → human sends)."""
    return {
        "name": "propose_reply",
        "description": (
            "Propose a reply to the chat message that triggered this run. The text "
            "is placed in the room's composer for the user to review, edit, and "
            "send — it is NEVER sent automatically. Provide the message text only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The proposed reply text to pre-fill the composer.",
                }
            },
            "required": ["text"],
        },
    }


def make_handler(
    ops: BridgeOps, *, base_system: str = "", draft_available: bool = False
) -> Handler:
    """Build the engine handler that runs each matched trigger via ``ops``."""

    def handler(card: TriggerCard, trigger: Trigger, event: Event) -> None:
        # Optional attention cue when the trigger fires — before either the rule
        # action or the agent turn, so the operator hears it as work begins. Purely
        # additive: a failure here never stops the action (ops.play_sound swallows).
        if trigger.sound:
            ops.play_sound(trigger.sound)
        if trigger.mode == "rule":
            # Optional single LLM pre-turn: interpret the event and fill action
            # params, exposed to the maps under the {{ _llm.* }} namespace. It only
            # extracts — it never chooses steps or branches (cliff guard).
            payload: dict[str, Any] = dict(event.payload)
            if trigger.pre_turn.strip():
                card.move(Column.TRIAGE)
                llm = ops.run_pre_turn(
                    card.id,
                    trigger.pre_turn,
                    render_event(event),
                    **({"model": trigger.model} if trigger.model else {}),
                )
                if llm:
                    payload["_llm"] = llm
                    card.log("pre-turn extracted: " + ", ".join(sorted(llm)))

            if not trigger.actions:
                # Single fixed runnable (the original rule path) — or, when `run`
                # names a host group ("group:<group>__<runnable>"), a fan-out over
                # every group member behind one grouped confirm.
                tool_input = trigger.render_args(payload)
                kind, gtarget, gname = parse_target(trigger.run)
                if kind == "host":
                    card.move(Column.RUNNING, tool=trigger.run, args=tool_input)
                    output = ops.run_rule_tool(
                        card.id, trigger.run, tool_input, trigger=event.source
                    )
                    label = (
                        f"/{gname} on {trigger.run.partition('__')[0]}"
                        if gname
                        else f"/{trigger.run}"
                    )
                else:
                    targets = ops.expand_targets(trigger.run)
                    label = f"/{gname} on group:{gtarget}"
                    card.move(
                        Column.RUNNING,
                        tool=f"{label} ({len(targets)} hosts)",
                        args=tool_input,
                    )
                    if not targets:
                        output = f"✗ No host in group '{gtarget}' has '{gname}'."
                    else:
                        calls = [(t, dict(tool_input)) for t in targets]
                        outputs = ops.run_rule_group(
                            card.id, calls, trigger=event.source
                        )
                        output = _fanout_block(targets, outputs)
                card.move(Column.DONE, result=output[:_RESULT_CAP])
                if trigger.post_to_chat:
                    # Mirror as a standard slash-command run card.
                    ops.mirror_to_chat(
                        "run",
                        f"{label}  (trigger: {trigger.name})",
                        output,
                        card_id=card.id,
                    )
                return

            # Declarative multi-runnable list (bounded — flat, no result piping).
            # Each step's run may target a host group, expanded to one call per
            # member here; the post-expansion total is bounded so a few group steps
            # can't balloon into a workflow-sized matrix.
            calls = []
            for step in trigger.actions:
                args = trigger.render_action_args(step, payload)
                for t in ops.expand_targets(step.run):
                    calls.append((t, dict(args)))
            if len(calls) > _MAX_FANOUT_CALLS:
                card.move(
                    Column.FAILED,
                    error=(
                        f"action list expands to {len(calls)} runs across the "
                        f"targeted host groups; the max is {_MAX_FANOUT_CALLS}. "
                        "Narrow the groups or split the trigger."
                    ),
                )
                return
            card.move(
                Column.RUNNING,
                tool=f"{len(calls)} runnables ({trigger.action_mode})",
            )
            if trigger.action_mode == "parallel":
                # One grouped confirm covers the whole batch.
                outputs = ops.run_rule_group(card.id, calls, trigger=event.source)
            else:
                outputs = [
                    ops.run_rule_tool(card.id, run_name, args, trigger=event.source)
                    for run_name, args in calls
                ]
            joined = "\n\n".join(
                f"• {name.split('__', 1)[-1]}:\n{out}"
                for (name, _), out in zip(calls, outputs)
            )
            card.move(Column.DONE, result=joined[:_RESULT_CAP])
            if trigger.post_to_chat:
                ops.mirror_to_chat(
                    "run",
                    f"{len(calls)} runnables (trigger: {trigger.name})",
                    joined,
                    card_id=card.id,
                )
            return

        draft = draft_available and trigger.reply.draft
        # A chat-message event drafts into the room composer (propose_reply, keyed
        # by the event's room); an Outlook mail event drafts a saved reply
        # (draft_reply, keyed by the message entry_id). Without the respective key
        # the bridge simply won't offer the tool (e.g. an Outlook reminder has no
        # entry_id).
        is_chat = event.source == "chat"
        reply_to = (
            (event.payload.get("entry_id") or None) if (draft and not is_chat) else None
        )
        room = (event.payload.get("room") or None) if (draft and is_chat) else None
        # A help-desk email draft replies *as the shared mailbox* (the queue
        # identity), the same as the human reply path; the bridge resolves it from
        # the event's recipient and returns "" for personal/other mail (drafts
        # from the operator). Chat drafts (propose_reply) never use it.
        from_mailbox = (
            ops.queue_mailbox_for(event.payload) if (draft and not is_chat) else ""
        ) or None
        system = compose_system(
            trigger,
            base_system,
            draft_available=draft,
            draft_kind="room" if is_chat else "mail",
            event=event,
        )
        card.move(Column.TRIAGE)
        summary = ops.run_agent_turn(
            card.id,
            system,
            render_event(event),
            draft=draft,
            reply_to=reply_to,
            room=room,
            from_mailbox=from_mailbox,
            # An empty allowlist means "any runnable" (the bridge treats None/empty
            # the same); a non-empty one scopes the agent to those tools.
            allowed_runnables=trigger.runnables or None,
            # Non-empty = restrict the built-in action/Outlook tools to this set.
            action_allow=trigger.action_allow or None,
            record=card.log,  # per-tool/gate steps onto the card timeline
            stage=lambda col: card.move(col),  # awaiting_human → running stages
            trigger=event.source,  # provenance recorded on each runnable the agent runs
            # The trigger's own model, when set; otherwise the [llm] model.
            **({"model": trigger.model} if trigger.model else {}),
        )
        # The agent's reply + any actions land on the card — a trigger turn has no
        # chat transcript, so this is where its output is visible.
        card.move(Column.DONE, result=summary) if summary else card.move(Column.DONE)
        if trigger.post_to_chat:
            # Mirror as an agent/chat card — the same shape as an LLM turn.
            label = f"Trigger '{trigger.name}' · {event.type}"
            ops.mirror_to_chat(
                "chat",
                label,
                summary or "(handled — no textual reply)",
                card_id=card.id,
            )

    return handler

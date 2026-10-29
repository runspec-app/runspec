"""Chat sessions — the unit of conversation in the Console.

Every conversation the console runs is a :class:`ChatSession`: an *interactive*
chat the operator types into, or an *automation* session opened for a trigger
fire / schedule fire. Sessions run concurrently; each owns its own rolling model
history, per-turn tags, "in context" exclusions, and the lock that serialises
its turns. Nothing here touches the Bridge — the module is pure (stdlib only)
so the history helpers are unit-testable without a bridge stub, and the Bridge
keeps a registry of these (``Bridge._sessions``) plus the per-turn mapping.

Identity rules (the invariant the UI relies on):

- an **interactive** session's id *is* its saved-conversation id, so its
  workspace dir (``workspace_dir(id)``) and its transcript file share the id;
- an **automation** session's id *is* the run card id, which the headless turn
  also uses as its ``chat_id`` — so a trigger/schedule card, its session, and
  its streamed ``runspec:*`` events all carry one id;
- an **mcp** session (one per external MCP client session) has its own id and
  holds many run cards — each card names it in its ``session`` field.

Sessions are in-memory only: a console restart starts with none (saved
conversations remain browsable via History and can be resumed into a new
session).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AUTOMATION_KINDS",
    "FINISHED_AUTOMATION_CAP",
    "HISTORY_MAX_MESSAGES",
    "ChatSession",
    "history_limits",
    "LABEL_LEN",
    "new_automation",
    "new_interactive",
    "new_mcp",
    "new_session_id",
    "prune_finished",
    "session_label",
]

#: Session kinds. ``chat`` is interactive; the rest are automation (read-only).
#: ``mcp`` is one external MCP client session's calls into the console's own
#: MCP server — long-lived (one chat per client session), so it is never
#: ``done`` and never pruned; the operator dismisses it.
AUTOMATION_KINDS = ("trigger", "schedule", "mcp")
#: How many *finished* automation sessions the registry keeps (oldest evicted).
FINISHED_AUTOMATION_CAP = 12
#: Max label length (an interactive chat is labelled by its first message).
LABEL_LEN = 60
#: An interactive chat's rolling history is trimmed once it passes this many
#: messages (``[llm] history_max_messages``) ...
HISTORY_MAX_MESSAGES = 60
#: ... and the chat's first message rides the trimmed history, clipped to this.
OPENING_NOTE_CHARS = 2000
_OPENING_HEAD = (
    "[Older turns of this chat were dropped to save space. It began with:]\n"
)
_OPENING_TAIL = "\n[End of the chat's first message. The current message follows.]\n\n"


def new_session_id() -> str:
    """A fresh interactive session id — the same shape as a saved-conversation
    id (``conversations.new_id``), since the two are one and the same."""
    return uuid.uuid4().hex[:16]


def first_line(text: str) -> str:
    """The first non-blank line of ``text``, stripped (``""`` when none)."""
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def session_label(text: str, limit: int = LABEL_LEN) -> str:
    """First line of ``text``, whitespace-collapsed and clipped for a chip."""
    line = " ".join(first_line(text).split())
    if len(line) <= limit:
        return line
    return line[: limit - 1].rstrip() + "…"


@dataclass
class ChatSession:
    id: str
    kind: str = "chat"
    label: str = ""
    interactive: bool = True
    created_at: float = field(default_factory=time.time)
    #: Adapter-native rolling history (raw SDK objects for tool turns — never
    #: serialised; the saved transcript is the provider-agnostic copy).
    history: list[dict[str, Any]] = field(default_factory=list)
    #: Per-message turn tag, aligned index-for-index with ``history``: the
    #: chat_id of the turn that appended each message. Lets a whole exchange be
    #: dropped from the model context (the "in context" toggle) by tag.
    turn_ids: list[str] = field(default_factory=list)
    #: chat_ids the operator toggled OUT of context; ``visible()`` skips them.
    excluded: set[str] = field(default_factory=set)
    #: Held for the whole of a turn — serialises sends on *this* session only.
    lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )
    #: ``idle`` | ``running`` | ``done`` (automation, after its one turn).
    state: str = "idle"
    #: chat_id of the in-flight turn ("" when idle).
    current_turn: str = ""
    #: Completed turns (an interactive label is set on the first).
    turns: int = 0
    #: The chat's first user message, remembered at the first trim so it can
    #: ride the trimmed history (the original task stays in view).
    opening: str = ""

    # ── identity ──────────────────────────────────────────────────────────

    @property
    def conversation_id(self) -> str:
        """The saved-conversation id this session persists to ("" for
        automation sessions, which are never saved)."""
        return self.id if self.interactive else ""

    @property
    def workspace_id(self) -> str:
        """The session-workspace directory name: the conversation id for a chat
        (so a resumed chat finds its files), ``trigger-<id>`` / ``schedule-<id>``
        for automation (the prefix drives the sweep's retention rule)."""
        return self.id if self.interactive else f"{self.kind}-{self.id}"

    def to_dict(self) -> dict[str, Any]:
        """The ``runspec:session`` event / ``list_chat_sessions`` row."""
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "interactive": self.interactive,
            "created_at": self.created_at,
            "state": self.state,
            "turn": self.current_turn,
            "turns": self.turns,
        }

    # ── history helpers (caller holds ``lock``) ─────────────────────────────

    def sync_tags(self, chat_id: str) -> None:
        """Tag every message appended during the current turn with ``chat_id``
        — all messages a turn appends belong to it. Pads ``turn_ids`` up to the
        history length (earlier messages keep their tags); truncates if it ran
        long (e.g. after a resume rebuilt the history)."""
        tags, hist = self.turn_ids, self.history
        if len(tags) > len(hist):
            del tags[len(hist) :]
        while len(tags) < len(hist):
            tags.append(chat_id)

    def visible(self) -> list[dict[str, Any]]:
        """The subset of ``history`` the model actually sees — every message
        except those of turns the operator toggled out of context. Excluding
        never mutates the stored history (the toggle stays reversible). A turn's
        messages are contiguous, so dropping by tag keeps tool_use/tool_result
        pairs intact; leading non-user / tool-result messages are then trimmed
        so the result opens on a plain user string (the API rejects a leading
        assistant or tool turn). Falls back to the full history if filtering
        would leave nothing."""
        if not self.excluded:
            return self.history
        tags = self.turn_ids
        kept = [
            msg
            for i, msg in enumerate(self.history)
            if (tags[i] if i < len(tags) else "") not in self.excluded
        ]
        while kept and not _is_user_text(kept[0]):
            kept.pop(0)
        return kept or self.history

    def trim(self, max_messages: int = HISTORY_MAX_MESSAGES, keep: int = 0) -> None:
        """Bound the rolling history so a long-lived session doesn't grow
        context without limit. Once it passes ``max_messages`` it is cut back to
        the newest ``keep`` (default half of ``max_messages``), then past any
        leading assistant / tool-result turn so it still starts on a plain user
        message (keeps tool_use/tool_result pairs intact). The aligned tags are
        trimmed in lockstep. Mutates in place.

        Cutting well below the cap, rather than to it, keeps the history's start
        unchanged for many turns: a cut every turn would change the prompt
        prefix every turn and throw away the model's prompt cache. The chat's
        first message is folded into the new first message so the original task
        isn't lost."""
        hist = self.history
        if len(hist) <= max_messages:
            return
        keep = keep if 0 < keep <= max_messages else max(1, max_messages // 2)
        if not self.opening and hist and _is_user_text(hist[0]):
            self.opening = str(hist[0]["content"])
        drop = len(hist) - keep
        del hist[:drop]
        while hist and not _is_user_text(hist[0]):
            hist.pop(0)
            drop += 1
        del self.turn_ids[:drop]
        if hist and self.opening:
            opening = self.opening
            if len(opening) > OPENING_NOTE_CHARS:
                opening = opening[: OPENING_NOTE_CHARS - 1].rstrip() + "…"
            hist[0] = {
                **hist[0],
                "content": f"{_OPENING_HEAD}{opening}{_OPENING_TAIL}{hist[0]['content']}",
            }

    def drop_turns(self, turn_ids: set[str]) -> int:
        """Physically remove every message of the given turns (and their tags),
        returning the count removed. Unlike ``visible()``'s non-destructive
        filter this is permanent — compaction uses it to bake in the current
        exclude decision. Mutates in place."""
        if not turn_ids:
            return 0
        tags = self.turn_ids
        keep_hist: list[dict[str, Any]] = []
        keep_tags: list[str] = []
        removed = 0
        for i, msg in enumerate(self.history):
            tag = tags[i] if i < len(tags) else ""
            if tag in turn_ids:
                removed += 1
                continue
            keep_hist.append(msg)
            keep_tags.append(tag)
        while keep_hist and not _is_user_text(keep_hist[0]):
            keep_hist.pop(0)
            keep_tags.pop(0)
        self.replace_history(keep_hist, keep_tags)
        return removed

    def replace_history(
        self, messages: list[dict[str, Any]], tags: list[str] | None = None
    ) -> None:
        """Swap the rolling history for ``messages`` **in place** (the list
        object survives — a running turn holds a reference to it). ``tags``
        defaults to the neutral ``""`` sentinel per message (never a real
        chat_id, so never excluded)."""
        self.history[:] = list(messages)
        self.turn_ids[:] = list(tags) if tags is not None else [""] * len(messages)

    def set_included(self, chat_id: str, included: bool) -> bool:
        """Toggle a turn in/out of the model context; returns the new state."""
        if included:
            self.excluded.discard(chat_id)
        else:
            self.excluded.add(chat_id)
        return chat_id not in self.excluded

    def owns_turn(self, chat_id: str) -> bool:
        return chat_id == self.current_turn or chat_id in self.turn_ids


def history_limits(config: dict[str, Any]) -> tuple[int, int]:
    """``(max_messages, keep)`` for :meth:`ChatSession.trim`, from the console
    config: ``[llm] history_max_messages`` (default ``HISTORY_MAX_MESSAGES``,
    floored at 4) and ``[llm] history_trim_to`` (default half the cap). The gap
    between the two keeps the history's start, and so the prompt cache,
    unchanged across the turns between cuts."""
    llm = (config or {}).get("llm", {}) or {}
    try:
        cap = int(llm.get("history_max_messages", HISTORY_MAX_MESSAGES))
    except (TypeError, ValueError):
        cap = HISTORY_MAX_MESSAGES
    cap = max(4, cap)
    try:
        keep = int(llm.get("history_trim_to", 0) or 0)
    except (TypeError, ValueError):
        keep = 0
    if not 0 < keep < cap:
        keep = cap // 2
    return cap, keep


def _is_user_text(msg: dict[str, Any]) -> bool:
    return msg.get("role") == "user" and isinstance(msg.get("content"), str)


def new_interactive(conversation_id: str = "", label: str = "") -> ChatSession:
    """An interactive chat session; ``conversation_id`` (a saved conversation
    being resumed) becomes its id, else a fresh one is minted."""
    return ChatSession(
        id=str(conversation_id or "") or new_session_id(),
        kind="chat",
        label=session_label(label),
        interactive=True,
    )


def new_automation(kind: str, chat_id: str, label: str) -> ChatSession:
    """A read-only automation session for a trigger / schedule card; its id is
    the card id (== the headless turn's chat_id)."""
    if kind not in AUTOMATION_KINDS:
        kind = "trigger"
    return ChatSession(
        id=str(chat_id),
        kind=kind,
        label=session_label(label),
        interactive=False,
    )


def new_mcp(label: str) -> ChatSession:
    """A read-only session for one external MCP client session: every tool call
    that client makes into the console's MCP server lands here as a run block."""
    return ChatSession(
        id=new_session_id(),
        kind="mcp",
        label=session_label(label),
        interactive=False,
    )


def prune_finished(
    sessions: dict[str, ChatSession], cap: int = FINISHED_AUTOMATION_CAP
) -> list[str]:
    """Evict the oldest *finished* automation sessions beyond ``cap`` from
    ``sessions`` (mutates the dict), returning the evicted ids. Interactive
    sessions are never evicted (the operator closes them)."""
    finished = sorted(
        (s for s in sessions.values() if not s.interactive and s.state == "done"),
        key=lambda s: s.created_at,
    )
    excess = len(finished) - max(0, cap)
    evicted: list[str] = []
    for s in finished[: max(0, excess)]:
        sessions.pop(s.id, None)
        evicted.append(s.id)
    return evicted

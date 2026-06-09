"""
triggers.py — the trigger engine: route events to runnable runs.

A ``Trigger`` binds an event (``source`` + ``event`` type, optionally narrowed by
a regex ``match``) to an action: either a deterministic runnable call
(``mode = "rule"``) or a headless agent turn (``mode = "agent"``). The
``TriggerEngine`` starts the shared event sources, matches each incoming
:class:`~runspec_console.events.Event` against the configured triggers, creates a
kanban ``TriggerCard`` per match, and runs the action — serialised per trigger so
two firings of the same trigger never overlap.

This module is bridge-agnostic and platform-independent: the *action* (running a
runnable / agent turn through the autonomy gate) is an injected ``handler``, and
the event source is built through an injected factory (``create_source`` by
default). That keeps the whole routing path testable with ``FakeSource`` and a
fake handler — no COM, no LLM, no pywebview. The bridge supplies the real handler
and card dispatch in step 4.

See docs/design/console-triggers.md (§5–§7, §9, §11).
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .events import Event, EventSource, create_source as _default_create_source

logger = logging.getLogger("runspec.console.triggers")

_VALID_MODES = frozenset({"agent", "rule"})
_VALID_AUTONOMY = frozenset({"autonomous", "confirm", "supervised", "manual"})

# Trigger-level keys — if one of these turns up inside [triggers.match] it's
# almost certainly a scalar written after the sub-table header (a TOML ordering
# slip), which would otherwise silently break matching.
_TRIGGER_KEYS = frozenset(
    {
        "name",
        "source",
        "event",
        "mode",
        "enabled",
        "autonomy",
        "post_to_chat",
        "rules",
        "run",
        "map",
        "reply",
        "match",
    }
)

# {{ field }} / {{field}} — single payload-key substitution in a rule map value.
_TEMPLATE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


# ── kanban columns ──────────────────────────────────────────────────────────


class Column:
    """Card lifecycle columns. Strings so they cross the bridge as-is."""

    TRIGGERED = "triggered"
    TRIAGE = "triage"
    AWAITING_HUMAN = "awaiting_human"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


_TERMINAL = frozenset({Column.DONE, Column.FAILED})


# ── reply settings ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReplySettings:
    """The ``[triggers.reply]`` block — the draft_reply headline feature (A5)."""

    draft: bool = False
    style: str = ""


# ── trigger model ───────────────────────────────────────────────────────────


@dataclass
class Trigger:
    """One configured event→action binding (a ``[[triggers]]`` table)."""

    name: str
    source: str
    event: str
    mode: str = "agent"
    enabled: bool = True
    autonomy: str = "confirm"
    post_to_chat: bool = False
    match: dict[str, str] = field(default_factory=dict)
    rules: str = ""  # agent mode: the LLM's per-trigger system prompt
    run: str = ""  # rule mode: "host__runnable"
    arg_map: dict[str, str] = field(default_factory=dict)  # rule mode: payload→arg
    reply: ReplySettings = field(default_factory=ReplySettings)

    # Compiled match patterns, filled in __post_init__.
    _patterns: dict[str, re.Pattern[str]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._patterns = {k: re.compile(v) for k, v in self.match.items()}

    @classmethod
    def from_config(cls, data: dict[str, Any]) -> Trigger:
        """Build (and validate) a Trigger from a parsed ``[[triggers]]`` table.

        Raises ``ValueError`` with a human-first message on bad config — the
        same spirit as runspec's own errors.
        """
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("trigger is missing a 'name'")

        def _req(key: str) -> str:
            val = str(data.get(key) or "").strip()
            if not val:
                raise ValueError(f"trigger '{name}' is missing '{key}'")
            return val

        source = _req("source")
        event = _req("event")

        mode = str(data.get("mode") or "agent").strip()
        if mode not in _VALID_MODES:
            raise ValueError(
                f"trigger '{name}' has unknown mode '{mode}' "
                f"(expected one of: {', '.join(sorted(_VALID_MODES))})"
            )

        autonomy = str(data.get("autonomy") or "confirm").strip()
        if autonomy not in _VALID_AUTONOMY:
            raise ValueError(
                f"trigger '{name}' has unknown autonomy '{autonomy}' "
                f"(expected one of: {', '.join(sorted(_VALID_AUTONOMY))})"
            )

        match = {str(k): str(v) for k, v in (data.get("match") or {}).items()}
        # Catch the classic TOML ordering mistake: a scalar key (e.g. `rules`)
        # written *after* [triggers.match] is parsed into the match table, which
        # silently makes the trigger never fire (a phantom field no payload has).
        misplaced = match.keys() & _TRIGGER_KEYS
        if misplaced:
            raise ValueError(
                f"trigger '{name}': [triggers.match] contains {sorted(misplaced)}, "
                "which look like misplaced trigger keys. Scalar keys (rules, run, "
                "mode, …) must come BEFORE the [triggers.match] / [triggers.reply] "
                "sub-tables in the TOML."
            )
        try:
            for pat in match.values():
                re.compile(pat)
        except re.error as exc:
            raise ValueError(
                f"trigger '{name}' has an invalid match regex: {exc}"
            ) from exc

        run = str(data.get("run") or "").strip()
        arg_map = {str(k): str(v) for k, v in (data.get("map") or {}).items()}
        if mode == "rule" and not run:
            raise ValueError(f"trigger '{name}' uses mode='rule' but has no 'run'")

        reply_raw = data.get("reply") or {}
        reply = ReplySettings(
            draft=bool(reply_raw.get("draft", False)),
            style=str(reply_raw.get("style") or ""),
        )

        return cls(
            name=name,
            source=source,
            event=event,
            mode=mode,
            enabled=bool(data.get("enabled", True)),
            autonomy=autonomy,
            post_to_chat=bool(data.get("post_to_chat", False)),
            match=match,
            rules=str(data.get("rules") or ""),
            run=run,
            arg_map=arg_map,
            reply=reply,
        )

    def matches(self, event: Event) -> bool:
        """True if this trigger should fire for ``event``.

        Source and event type must equal; then every ``match`` pattern must
        ``search`` its payload field (a missing field never matches). An empty
        ``match`` matches every event of the right source/type.
        """
        if not self.enabled:
            return False
        if event.source != self.source or event.type != self.event:
            return False
        for field_name, pattern in self._patterns.items():
            value = event.payload.get(field_name)
            if value is None or not pattern.search(str(value)):
                return False
        return True

    def render_args(self, payload: dict[str, Any]) -> dict[str, str]:
        """Rule-mode payload→arg mapping: render ``{{ field }}`` from the payload.

        Returns the raw arg strings; type coercion/validation happens when the
        runnable actually runs (the existing runspec run path), so we don't
        duplicate the validator here.
        """

        def _sub(m: re.Match[str]) -> str:
            return str(payload.get(m.group(1), ""))

        return {arg: _TEMPLATE.sub(_sub, tmpl) for arg, tmpl in self.arg_map.items()}


# ── kanban card ─────────────────────────────────────────────────────────────


@dataclass
class TriggerCard:
    """A single event instance flowing across the board. ``move()`` notifies the
    UI via the engine's on_card hook."""

    id: str
    trigger: str
    source: str
    event_type: str
    summary: str
    column: str = Column.TRIGGERED
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    _on_change: Callable[[TriggerCard], None] | None = field(default=None, repr=False)

    def move(self, column: str, **detail: Any) -> None:
        """Move the card to ``column``, merge ``detail``, and fire the hook."""
        self.column = column
        if detail:
            self.detail.update(detail)
        if self._on_change is not None:
            self._on_change(self)

    @property
    def terminal(self) -> bool:
        return self.column in _TERMINAL

    def to_dict(self) -> dict[str, Any]:
        """Serialisable snapshot for the bridge ``runspec:trigger_card`` event."""
        return {
            "card_id": self.id,
            "trigger": self.trigger,
            "source": self.source,
            "event_type": self.event_type,
            "summary": self.summary,
            "column": self.column,
            "detail": self.detail,
            "created_at": self.created_at,
        }


def _summarize(event: Event) -> str:
    """A short human label for a card, best-effort from common payload fields."""
    p = event.payload
    for key in ("subject", "title", "summary", "message"):
        if p.get(key):
            text = str(p[key])
            return text if len(text) <= 120 else text[:117] + "…"
    return f"{event.source}:{event.type}"


# ── the engine ──────────────────────────────────────────────────────────────

# handler(card, trigger, event) performs the action (rule/agent) through the
# autonomy gate, driving intermediate columns; the engine guarantees a terminal
# column (DONE on return, FAILED on exception) if the handler left one unset.
Handler = Callable[[TriggerCard, Trigger, Event], None]


class TriggerEngine:
    """Owns the live sources and routes their events to trigger runs."""

    def __init__(
        self,
        triggers: list[Trigger],
        *,
        handler: Handler,
        on_card: Callable[[TriggerCard], None] | None = None,
        create_source: Callable[[str], EventSource | None] = _default_create_source,
        executor: Executor | None = None,
        max_workers: int = 2,
    ) -> None:
        self._triggers = list(triggers)
        self._handler = handler
        self._on_card = on_card
        self._create_source = create_source
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="trigger"
        )
        self._owns_executor = executor is None
        self._sources: dict[str, EventSource] = {}
        self._trigger_locks: dict[str, threading.Lock] = {
            t.name: threading.Lock() for t in self._triggers
        }
        self._lock = threading.Lock()
        self._started = False

    # -- lifecycle --

    def start(self) -> list[str]:
        """Start one shared source per distinct source id used by an *enabled*
        trigger. Returns a list of human-readable problems (unknown or
        unavailable sources) — non-fatal, so one bad source can't block the rest.
        """
        problems: list[str] = []
        with self._lock:
            if self._started:
                return problems
            wanted = {t.source for t in self._triggers if t.enabled}
            for source_id in sorted(wanted):
                src = self._create_source(source_id)
                if src is None:
                    problems.append(f"unknown event source '{source_id}'")
                    continue
                ok, reason = src.available()
                if not ok:
                    problems.append(f"source '{source_id}' unavailable: {reason}")
                    continue
                src.start(self._emit)
                self._sources[source_id] = src
            self._started = True
        return problems

    def stop(self) -> None:
        with self._lock:
            for src in self._sources.values():
                try:
                    src.stop()
                except Exception:
                    logger.exception(
                        "error stopping source %s", getattr(src, "id", "?")
                    )
            self._sources.clear()
            self._started = False
        if self._owns_executor:
            self._executor.shutdown(wait=False)

    # -- routing --

    def _emit(self, event: Event) -> None:
        """Source callback (the ``EmitFn``): route an event, discarding futures.

        May be called from a source's own thread.
        """
        self._route(event)

    def _route(self, event: Event) -> Future[None] | None:
        """Route an event to every matching trigger.

        Returns the Future of the last submitted run (handy for tests with a
        synchronous executor); ``None`` if nothing matched.
        """
        last: Future[None] | None = None
        for trigger in self._triggers:
            if trigger.matches(event):
                last = self._dispatch(trigger, event)
        return last

    def _dispatch(self, trigger: Trigger, event: Event) -> Future[None]:
        card = TriggerCard(
            id=uuid.uuid4().hex[:12],
            trigger=trigger.name,
            source=event.source,
            event_type=event.type,
            summary=_summarize(event),
            _on_change=self._on_card,
        )
        if self._on_card is not None:
            self._on_card(card)  # initial TRIGGERED card
        logger.info(
            "trigger '%s' matched %s:%s — card %s",
            trigger.name,
            event.source,
            event.type,
            card.id,
        )
        return self._executor.submit(self._run, card, trigger, event)

    def _run(self, card: TriggerCard, trigger: Trigger, event: Event) -> None:
        """Run one card's action, serialised against other runs of the same
        trigger, with a guaranteed terminal column."""
        lock = self._trigger_locks.setdefault(trigger.name, threading.Lock())
        with lock:
            try:
                self._handler(card, trigger, event)
                if not card.terminal:
                    card.move(Column.DONE)
            except Exception as exc:  # noqa: BLE001 — surface on the card, don't crash the worker
                logger.exception("trigger '%s' card %s failed", trigger.name, card.id)
                card.move(Column.FAILED, error=str(exc))


def load_triggers(configs: list[dict[str, Any]]) -> tuple[list[Trigger], list[str]]:
    """Parse a list of ``[[triggers]]`` tables.

    Returns ``(triggers, errors)``: a bad table is skipped with a message rather
    than sinking the whole set, so one typo doesn't disable every trigger.
    """
    triggers: list[Trigger] = []
    errors: list[str] = []
    for raw in configs:
        try:
            triggers.append(Trigger.from_config(raw))
        except ValueError as exc:
            errors.append(str(exc))
    return triggers, errors

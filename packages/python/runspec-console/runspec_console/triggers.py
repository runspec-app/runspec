"""
triggers.py — the trigger engine: route events to runnable runs.

A ``Trigger`` binds an event (``source`` + ``event`` type, optionally narrowed by
a regex ``match``) to an action: either a deterministic runnable call
(``mode = "rule"``) or a headless agent turn (``mode = "agent"``). The
``TriggerEngine`` starts the shared event sources, matches each incoming
:class:`~runspec_console.events.Event` against the configured triggers, creates a
run-inbox ``TriggerCard`` per match, and runs the action — serialised per trigger
so two firings of the same trigger never overlap.

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
from datetime import datetime
from typing import Any, Callable

from .events import Event, EventSource, create_source as _default_create_source

logger = logging.getLogger("runspec.console.triggers")

_VALID_MODES = frozenset({"agent", "rule"})
_VALID_AUTONOMY = frozenset({"autonomous", "confirm", "supervised", "manual"})
_VALID_ACTION_MODES = frozenset({"sequential", "parallel"})
# A rule trigger's declarative action list is bounded on purpose — it's a flat
# fire-and-collect list, never a workflow engine. The cap keeps it obviously so.
_MAX_ACTIONS = 10

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
        "ignore_rota",
        "once_per_thread",
        "rules",
        "run",
        "runnables",
        "map",
        "reply",
        "match",
        "actions",
        "action_mode",
        "pre_turn",
        "action_allow",
        "claim",
        "claim_folder",
    }
)

# {{ field }} / {{ ns.field }} — payload-key substitution in a rule map value. The
# dotted form resolves one level into a nested dict (used for the optional LLM
# pre-turn output, exposed under the ``_llm`` namespace).
_TEMPLATE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def _render_template(tmpl: str, payload: dict[str, Any]) -> str:
    """Substitute ``{{ field }}`` / ``{{ ns.field }}`` against ``payload``."""

    def _sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if "." in key:
            head, _, tail = key.partition(".")
            ns = payload.get(head)
            return str(ns.get(tail, "")) if isinstance(ns, dict) else ""
        return str(payload.get(key, ""))

    return _TEMPLATE.sub(_sub, tmpl)


# ── working-hours rota ────────────────────────────────────────────────────────
# A global weekly schedule (in local time) that gates when triggers may fire, so
# automation only runs while someone's around to handle confirms. Off by default.

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_hhmm(value: str) -> int:
    """Minutes-since-midnight for an 'HH:MM' string (0 on a bad value)."""
    try:
        hh, mm = str(value).split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return 0


def validate_rota(rota: dict[str, Any]) -> list[str]:
    """Problems with a rota dict, or ``[]`` when it is saveable. The runtime
    consumers (``within_rota``/``_parse_hhmm``) tolerate any shape by design, so
    this is the pre-flight for callers (the agent's ``manage_rota``) that must
    refuse bad input with a message instead of silently defaulting it.
    """
    errors: list[str] = []
    source = rota.get("source")
    if source not in (None, "", "console", "outlook"):
        errors.append('\'source\' must be "console" or "outlook"')
    known = set(_DAYS) | {
        "enabled",
        "source",
        "shift_marker",
        "hold_during_meetings",
        "lock_suspend",
    }
    for key in rota:
        if key not in known:
            errors.append(f"unknown rota key '{key}'")
    hhmm = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
    for day in _DAYS:
        entry = rota.get(day)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            errors.append(
                f'\'{day}\' must be a table like {{on = true, start = "09:00", end = "17:30"}}'
            )
            continue
        for bound in ("start", "end"):
            value = entry.get(bound)
            if value is not None and not hhmm.match(str(value)):
                errors.append(f"'{day}.{bound}' must be \"HH:MM\" (got {value!r})")
    return errors


def within_rota(rota: dict[str, Any] | None, now: datetime) -> bool:
    """Whether ``now`` (local) falls inside the working-hours rota.

    No rota, or ``enabled = false`` → always True (no gating — the default, so
    nothing changes until a rota is set up). For an enabled rota, the weekday
    must be ``on`` and the time within ``[start, end]`` (an overnight window where
    ``end < start`` is supported).
    """
    if not rota or not rota.get("enabled"):
        return True
    day = rota.get(_DAYS[now.weekday()])
    if not isinstance(day, dict) or not day.get("on", False):
        return False
    start = _parse_hhmm(day.get("start", "00:00"))
    end = _parse_hhmm(day.get("end", "23:59"))
    cur = now.hour * 60 + now.minute
    return start <= cur <= end if start <= end else (cur >= start or cur <= end)


# ── run lifecycle statuses ────────────────────────────────────────────────────


class Column:
    """Card lifecycle columns. Strings so they cross the bridge as-is."""

    TRIGGERED = "triggered"
    TRIAGE = "triage"
    AWAITING_HUMAN = "awaiting_human"
    # A claim-enabled trigger parks here until an operator clicks Claim. Non-
    # terminal (the run hasn't happened) but the engine does not force it to DONE
    # on the parking dispatch — it sits until claimed or claimed-elsewhere.
    AWAITING_CLAIM = "awaiting_claim"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


_TERMINAL = frozenset({Column.DONE, Column.FAILED})
# Columns a card may rest in without the engine forcing a terminal DONE when the
# handler returns / the dispatch parks (see TriggerEngine._dispatch / _run).
_PARKED = frozenset({Column.AWAITING_CLAIM})


# ── reply settings ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReplySettings:
    """The ``[triggers.reply]`` block — the draft_reply headline feature (A5)."""

    draft: bool = False
    style: str = ""


@dataclass(frozen=True)
class ActionStep:
    """One runnable call in a rule-mode trigger's declarative action list — a
    ``[[triggers.<name>.actions]]`` entry: a ``run`` ("host__runnable", or
    "group:<group>__<runnable>" to fan out to a host group) plus an optional
    payload→arg ``map``."""

    run: str
    arg_map: dict[str, str] = field(default_factory=dict)


# A rule/action ``run`` may target a host GROUP instead of one host, written as
# ``group:<group>__<runnable>``. The host resolution lives in the bridge (it owns
# the host list); this pure classifier just splits the string for the runner and
# for labels/validation.
_GROUP_PREFIX = "group:"


def parse_target(run: str) -> tuple[str, str, str]:
    """Classify a rule/action ``run`` target into ``(kind, target, runnable)``.

    ``"group:<group>__<runnable>"`` → ``("group", "<group>", "<runnable>")`` (fan
    out to every member of the host group). Anything else is a plain single host:
    ``"<host>__<runnable>"`` → ``("host", "<run>", "<runnable>")`` where ``target``
    is the unchanged tool-name string and ``runnable`` is the tail (for labels)."""
    s = str(run or "").strip()
    if s.startswith(_GROUP_PREFIX):
        group, _, runnable = s[len(_GROUP_PREFIX) :].partition("__")
        return "group", group.strip(), runnable.strip()
    _, _, runnable = s.partition("__")
    return "host", s, runnable.strip()


# ── trigger model ───────────────────────────────────────────────────────────


@dataclass
class Trigger:
    """One configured event→action binding (a ``[triggers.<name>]`` table)."""

    name: str
    source: str
    event: str
    mode: str = "agent"
    enabled: bool = True
    autonomy: str = "confirm"
    post_to_chat: bool = False
    ignore_rota: bool = False  # fire even outside working hours (escape hatch)
    # Fire only for the FIRST matching event in a conversation/thread, then skip
    # later messages in it (e.g. don't re-run on every email reply). Keyed on the
    # event's thread id (conversation_id / thread_id); fail-open when the payload
    # carries no thread id (fires, since we can't tell it's a reply). Tri-state:
    # ``None`` inherits the engine default (the ``[mailbox]`` setting); ``True`` /
    # ``False`` override it for this trigger. Resolution: trigger → mailbox default
    # → library default (False) — the same shape as ``autonomy``.
    once_per_thread: bool | None = None
    match: dict[str, str] = field(default_factory=dict)
    rules: str = ""  # agent mode: the LLM's per-trigger system prompt
    run: str = ""  # rule mode: "host__runnable"
    # agent mode: an allowlist of runnable names the agent may call (empty = all).
    # The third point on the rule↔agent spectrum: rule = one fixed runnable, agent
    # = any runnable, agent+runnables = the LLM extracts/decides but only over
    # these tools. Each entry matches a runnable's bare name or its "host__name".
    runnables: list[str] = field(default_factory=list)
    arg_map: dict[str, str] = field(default_factory=dict)  # rule mode: payload→arg
    # Rule mode: a bounded declarative list of runnable calls (the multi-runnable
    # alternative to the single ``run``; mutually exclusive with it). ``action_mode``
    # runs them all in order ("sequential") or all at once with one grouped confirm
    # ("parallel"). This is fire-and-collect — no branching, no step-result piping —
    # so it stays under the "no workflow engine" line (see console-triggers.md §1).
    actions: list[ActionStep] = field(default_factory=list)
    action_mode: str = "sequential"
    # agent mode: an allowlist of built-in action tool names this trigger may use
    # (e.g. "create_document", "outlook_draft"). Empty = the bridge default set.
    action_allow: list[str] = field(default_factory=list)
    # Optional single headless LLM pre-turn (rule mode) that interprets the event
    # and fills action args; its output is exposed to the maps under {{ _llm.* }}.
    # One model call, no runnable tools — it extracts, it does not act or branch.
    pre_turn: str = ""
    reply: ReplySettings = field(default_factory=ReplySettings)
    # Shared-mailbox claim gate (outlook/new_mail only). When ``claim`` is true a
    # matched mail does NOT auto-run: its card parks in ``awaiting_claim`` until an
    # operator clicks Claim; the winning console moves the item into ``claim_folder``
    # (which must already exist) and then runs the trigger's normal action. This
    # coordinates several consoles watching one shared folder — exactly one wins.
    # See docs/design/email-claim-move.md.
    claim: bool = False
    claim_folder: str = ""
    # Optional audio cue played when this trigger fires (both rule and agent
    # modes) — a named sound id (from runspec_sounds.toml), a bundled cue name, or
    # a full "sound:"/"synth:"/"system:"/"file:" token. Best-effort + Windows-only,
    # so it never affects whether the action runs; resolved via sounds.play_identifier.
    sound: str = ""

    # Compiled match patterns, filled in __post_init__.
    _patterns: dict[str, re.Pattern[str]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._patterns = {k: re.compile(v) for k, v in self.match.items()}

    @classmethod
    def from_config(cls, data: dict[str, Any], *, ai_enabled: bool = True) -> Trigger:
        """Build (and validate) a Trigger from a parsed ``[triggers.<name>]`` table.

        The trigger's ``name`` is the table key (injected by
        :func:`config.read_triggers`), so a config-sourced trigger always has
        one. Raises ``ValueError`` with a human-first message on bad config —
        the same spirit as runspec's own errors.

        ``ai_enabled=False`` (the console's ``[console] ai`` off-switch) refuses
        an agent-mode trigger and a rule trigger with an LLM ``pre_turn`` — the
        trigger is reported invalid rather than silently degraded to a plain
        rule, so a seeded file makes its AI dependence visible.
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
        # source="schedule" is just another source here: the schedule (in the
        # Schedules tab) emits a named event, and this trigger matches it by
        # event type + match rules, exactly like a mail trigger.
        event = _req("event")

        mode = str(data.get("mode") or "agent").strip()
        if mode not in _VALID_MODES:
            raise ValueError(
                f"trigger '{name}' has unknown mode '{mode}' "
                f"(expected one of: {', '.join(sorted(_VALID_MODES))})"
            )
        if not ai_enabled:
            if mode != "rule":
                raise ValueError(
                    f"trigger '{name}' is an agent-mode trigger, but AI features "
                    'are not available in this console — set mode = "rule" for it '
                    "to run"
                )
            if str(data.get("pre_turn") or "").strip():
                raise ValueError(
                    f"trigger '{name}' has an LLM pre_turn step, but AI features "
                    "are not available in this console"
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

        # Declarative multi-runnable list (rule mode): [[triggers.<name>.actions]].
        actions_raw = data.get("actions")
        actions: list[ActionStep] = []
        if actions_raw is not None:
            if not isinstance(actions_raw, list):
                raise ValueError(
                    f"trigger '{name}': 'actions' must be a list of {{run, map}} "
                    "tables ([[triggers.<name>.actions]])."
                )
            for i, item in enumerate(actions_raw):
                if not isinstance(item, dict):
                    raise ValueError(
                        f"trigger '{name}': actions[{i}] must be a table with a 'run'."
                    )
                step_run = str(item.get("run") or "").strip()
                if not step_run:
                    raise ValueError(
                        f"trigger '{name}': actions[{i}] is missing 'run'."
                    )
                step_map = {str(k): str(v) for k, v in (item.get("map") or {}).items()}
                actions.append(ActionStep(run=step_run, arg_map=step_map))
            if len(actions) > _MAX_ACTIONS:
                raise ValueError(
                    f"trigger '{name}': too many actions ({len(actions)}); the max "
                    f"is {_MAX_ACTIONS}. A trigger fires one bounded action list, "
                    "not a workflow."
                )

        action_mode = str(data.get("action_mode") or "sequential").strip()
        if action_mode not in _VALID_ACTION_MODES:
            raise ValueError(
                f"trigger '{name}' has unknown action_mode '{action_mode}' "
                f"(expected one of: {', '.join(sorted(_VALID_ACTION_MODES))})"
            )
        if actions and mode != "rule":
            raise ValueError(
                f"trigger '{name}': 'actions' is a rule-mode declarative list; "
                "agent mode uses 'runnables' (a tool allowlist)."
            )
        if mode == "rule":
            if run and actions:
                raise ValueError(
                    f"trigger '{name}': use either 'run' (one runnable) or 'actions' "
                    "(a list), not both."
                )
            if not run and not actions:
                raise ValueError(
                    f"trigger '{name}' uses mode='rule' but has no 'run' or 'actions'"
                )

        runnables_raw = data.get("runnables")
        if runnables_raw is None:
            runnables: list[str] = []
        elif isinstance(runnables_raw, list):
            runnables = [str(x).strip() for x in runnables_raw if str(x).strip()]
        else:
            raise ValueError(
                f"trigger '{name}': 'runnables' must be a list of runnable names, "
                'e.g. runnables = ["web01__check-memory", "restart-service"]'
            )
        if runnables and mode == "rule":
            raise ValueError(
                f"trigger '{name}': 'runnables' is an agent tool allowlist (mode="
                "'agent'); rule mode runs the single runnable named by 'run'."
            )

        action_allow_raw = data.get("action_allow")
        if action_allow_raw is None:
            action_allow: list[str] = []
        elif isinstance(action_allow_raw, list):
            action_allow = [str(x).strip() for x in action_allow_raw if str(x).strip()]
        else:
            raise ValueError(
                f"trigger '{name}': 'action_allow' must be a list of action tool names."
            )

        # Tri-state: absent → None (inherit the mailbox default); present → an
        # explicit per-trigger override.
        opt_raw = data.get("once_per_thread")
        once_per_thread = bool(opt_raw) if opt_raw is not None else None

        reply_raw = data.get("reply") or {}
        reply = ReplySettings(
            draft=bool(reply_raw.get("draft", False)),
            style=str(reply_raw.get("style") or ""),
        )

        # Shared-mailbox claim gate. Only meaningful for the Outlook new_mail
        # source; when on it needs a folder to scope to (match.folder) and a
        # destination folder to move the claimed item into.
        claim = bool(data.get("claim", False))
        claim_folder = str(data.get("claim_folder") or "").strip()
        if claim:
            if source != "outlook" or event != "new_mail":
                raise ValueError(
                    f"trigger '{name}': 'claim' is only supported for "
                    "source='outlook', event='new_mail'."
                )
            if not str(match.get("folder") or "").strip():
                raise ValueError(
                    f"trigger '{name}': a claim trigger needs a folder to match on — "
                    "set [triggers.match] folder to the watched shared folder."
                )
            if not claim_folder:
                raise ValueError(
                    f"trigger '{name}': a claim trigger needs 'claim_folder' — the "
                    "existing folder a claimed message is moved into."
                )

        return cls(
            name=name,
            source=source,
            event=event,
            mode=mode,
            enabled=bool(data.get("enabled", True)),
            autonomy=autonomy,
            post_to_chat=bool(data.get("post_to_chat", False)),
            ignore_rota=bool(data.get("ignore_rota", False)),
            once_per_thread=once_per_thread,
            match=match,
            rules=str(data.get("rules") or ""),
            run=run,
            runnables=runnables,
            arg_map=arg_map,
            actions=actions,
            action_mode=action_mode,
            action_allow=action_allow,
            pre_turn=str(data.get("pre_turn") or ""),
            reply=reply,
            claim=claim,
            claim_folder=claim_folder,
            sound=str(data.get("sound") or "").strip(),
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
        return {
            arg: _render_template(tmpl, payload) for arg, tmpl in self.arg_map.items()
        }

    def render_action_args(
        self, step: ActionStep, payload: dict[str, Any]
    ) -> dict[str, str]:
        """Render one declarative action step's ``map`` against the payload (which
        may include the ``_llm`` namespace from a pre-turn). Same contract as
        :meth:`render_args`."""
        return {
            arg: _render_template(tmpl, payload) for arg, tmpl in step.arg_map.items()
        }


# ── run card ──────────────────────────────────────────────────────────────────


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
    # Timestamped audit trail of stage changes + notable steps (the "what
    # happened in each lane" record). Each entry: {"ts": float, "text": str}.
    timeline: list[dict[str, Any]] = field(default_factory=list)
    _on_change: Callable[[TriggerCard], None] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.timeline:
            self.timeline.append({"ts": self.created_at, "text": self.column})

    def move(self, column: str, **detail: Any) -> None:
        """Move the card to ``column``, merge ``detail``, record it, fire the hook."""
        self.column = column
        if detail:
            self.detail.update(detail)
        self.timeline.append({"ts": time.time(), "text": column})
        if self._on_change is not None:
            self._on_change(self)

    def log(self, text: str) -> None:
        """Append a free-text step to the timeline (e.g. a tool run / gate) and
        fire the hook. Used to record what happened between stage changes."""
        self.timeline.append({"ts": time.time(), "text": text})
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
            "timeline": self.timeline,
        }


def _summarize(event: Event) -> str:
    """A short human label for a card, best-effort from common payload fields."""
    p = event.payload
    for key in ("subject", "title", "summary", "message"):
        if p.get(key):
            text = str(p[key])
            return text if len(text) <= 120 else text[:117] + "…"
    return f"{event.source}:{event.type}"


# ── once-per-thread dedup ─────────────────────────────────────────────────────

# Payload keys that identify a conversation/thread, tried in order. `outlook`
# supplies `conversation_id`; a chat-style source may supply `thread_id`.
_THREAD_KEYS = ("conversation_id", "thread_id")


def thread_key(payload: dict[str, Any]) -> str:
    """The conversation/thread id from an event payload (``""`` when none).

    A trigger with ``once_per_thread`` keys its dedup on this. Empty means the
    source can't thread the event, so the engine fails open (fires) rather than
    guessing it's a reply."""
    for key in _THREAD_KEYS:
        val = payload.get(key)
        if val:
            return str(val)
    return ""


class _MemoryThreadStore:
    """In-memory ``ThreadDedupLike``: remembers thread ids per trigger for one
    process. Bounded per trigger (oldest dropped). The bridge injects a durable
    store; this default keeps the engine correct (and testable) without one."""

    def __init__(self, cap: int = 5000) -> None:
        self._cap = cap
        self._seen: dict[str, dict[str, None]] = {}  # trigger → insertion-ordered ids
        self._lock = threading.Lock()

    def already_fired(self, trigger: str, key: str) -> bool:
        """True if ``trigger`` already fired for thread ``key`` (recording it as
        seen if not). The check + record is atomic so two near-simultaneous
        replies can't both slip through."""
        with self._lock:
            ids = self._seen.setdefault(trigger, {})
            if key in ids:
                return True
            ids[key] = None
            if len(ids) > self._cap:
                for old in list(ids)[: len(ids) - self._cap]:
                    del ids[old]
            return False


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
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_source_event: Callable[[Event], None] | None = None,
        feed_gate: Callable[[Event], bool] | None = None,
        create_source: Callable[[str], EventSource | None] = _default_create_source,
        executor: Executor | None = None,
        max_workers: int = 2,
        clock_ok: Callable[[], bool] = lambda: True,
        lock_ok: Callable[[], bool] = lambda: True,
        external_sources: set[str] | None = None,
        always_start: set[str] | None = None,
        thread_store: Any | None = None,
        default_once_per_thread: bool = False,
    ) -> None:
        self._triggers = list(triggers)
        self._handler = handler
        self._on_card = on_card
        # Called once per incoming event with its disposition (matched / skipped
        # by rota / no match) — the audit feed of everything the engine sees.
        self._on_event = on_event
        # Called with every *raw* event before routing (full payload, regardless
        # of whether any trigger matches) — the bridge uses it to fold a
        # shared-mailbox new_mail event onto a queue card, the email analog of the
        # chat source's emit. Best-effort: a failing hook never blocks routing.
        self._on_source_event = on_source_event
        # A predicate the host supplies to suppress routing for an event (no
        # card, no trigger run, no audit record) — checked once per event, after
        # ``on_source_event`` so the host can fold the event onto a queue card
        # first and decide from the folded state. The email claim gate uses it:
        # a shared-mailbox new_mail only routes once this console owns the thread,
        # mirroring how an unclaimed chat message never reaches feed(). Fail-open:
        # a raising gate never blocks routing. Default (None) routes everything.
        self._feed_gate = feed_gate
        self._create_source = create_source
        # Source ids the engine must NOT start itself — they're owned elsewhere and
        # push events in via feed() (e.g. the always-on chat source the bridge
        # manages across engine restarts). Their triggers still match normally.
        self._external = set(external_sources or ())
        # Source ids to start even when no enabled trigger names them — e.g.
        # "outlook" when a shared mailbox is mapped to a queue but the desk runs no
        # mail/reminder trigger, so new_mail still flows in to surface queue cards.
        self._always_start = set(always_start or ())
        # Returns True when triggers may fire now (the working-hours rota gate).
        # Default always-True = no gating.
        self._clock_ok = clock_ok
        # Lock auto-suspend gate: True when the operator is present (screen
        # unlocked). A **presence governance control** — unlike the rota it is
        # absolute: ``ignore_rota`` does NOT bypass it, so no reactive trigger acts
        # while the workstation is unattended. Events from the schedule source are
        # exempt (a pre-authorised, time-based job is not a reaction-while-away).
        # Default always-True = no gating.
        self._lock_ok = lock_ok
        # Remembers which threads a once_per_thread trigger already fired for. The
        # default is process-local; the bridge injects a durable store so the dedup
        # survives a restart. Only needs ``already_fired(trigger, key) -> bool``.
        self._thread_store = thread_store or _MemoryThreadStore()
        # The reply-suppression default a trigger inherits when its own
        # once_per_thread is unset — sourced from the ``[mailbox]`` config.
        self._default_once_per_thread = default_once_per_thread
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
        # Claim gate: a claim-enabled trigger parks its card here (keyed by card id)
        # instead of running, until the bridge resolves it. ``_claim_granted`` marks
        # cards a claim was won for, so the re-dispatched run skips the gate.
        self._parked_claims: dict[str, tuple[TriggerCard, Trigger, Event]] = {}
        self._claim_granted: set[str] = set()

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
            # Externally-owned sources (e.g. chat) aren't started here — the owner
            # runs them and pushes events through feed(); their triggers still match.
            wanted = (
                {t.source for t in self._triggers if t.enabled} | self._always_start
            ) - self._external
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

    def feed(self, event: Event) -> Future[None] | None:
        """Route an event from an *externally-owned* source (one declared in
        ``external_sources`` and run by the caller, not started by the engine).
        Same path as a built-in source's emit — match, rota-gate, dispatch — so a
        chat message the bridge owns fires triggers identically. Safe to call from
        another thread; a no-op-ish route when the engine matches nothing."""
        return self._route(event)

    def would_match(self, event: Event) -> bool:
        """Whether any configured trigger matches ``event`` (source + event type +
        ``match`` patterns), without dispatching it. Lets a host ask "is this
        triggered?" — the email channel surfaces a ticket only for mail a trigger
        matches, so the help-desk inbox isn't a mirror of the whole mailbox."""
        return any(t.matches(event) for t in self._triggers)

    def fire_test(self, name: str, event: Event) -> Future[None] | None:
        """Dispatch one named trigger with a synthetic ``event``, for the Triggers
        tab's "Trigger now" button — so a user can exercise a trigger without
        waiting for a real event. Deliberately bypasses ``match``, the rota gate,
        and once_per_thread dedup: the user explicitly chose *this* trigger to
        test, so the run should always happen. Also bypasses the claim gate — a
        synthetic test event has no real message to move, so it runs directly.
        Records an audit entry (matched) and returns the run's Future. Returns
        ``None`` if no such trigger."""
        trigger = next((t for t in self._triggers if t.name == name), None)
        if trigger is None:
            return None
        self._record_event(event, [name], [], [])
        return self._dispatch(trigger, event, claim_gate=False)

    def fire_paste(self, name: str, event: Event) -> dict[str, Any]:
        """Dispatch one named ``source = "paste"`` trigger with an operator-pasted
        ``event``, for the Triggers tab's per-row paste action.

        Unlike :meth:`fire_test`, this is a **real run**, not a test: it applies
        the trigger's own gates — ``match`` (so the operator sees why a paste that
        doesn't fit the filter was ignored), the working-hours rota (honouring
        ``ignore_rota``), and ``once_per_thread`` dedup — scoped to just this one
        trigger. It **bypasses the lock gate** only: a human actively pasting is
        present by definition (the same reason ``_route`` pre-authorises
        ``source == "schedule"``).

        Returns ``{"ok": bool, "skipped": <reason|None>}`` so the UI can explain a
        no-run. Records the disposition to the audit feed either way.
        """
        trigger = next((t for t in self._triggers if t.name == name), None)
        if trigger is None:
            return {"ok": False, "skipped": f"No trigger named {name!r}."}
        if not trigger.matches(event):
            self._record_event(event, [], [], [])
            return {
                "ok": False,
                "skipped": "The pasted text didn't match this trigger's filter.",
            }
        if not self._clock_ok() and not trigger.ignore_rota:
            self._record_event(event, [], [name], [])
            return {"ok": False, "skipped": "Outside working hours (rota)."}
        once = (
            trigger.once_per_thread
            if trigger.once_per_thread is not None
            else self._default_once_per_thread
        )
        if once:
            key = thread_key(event.payload)
            if key and self._thread_store.already_fired(trigger.name, key):
                self._record_event(event, [], [], [name])
                return {"ok": False, "skipped": "Already handled this thread."}
        self._record_event(event, [name], [], [])
        self._dispatch(trigger, event)
        return {"ok": True, "skipped": None}

    def _route(self, event: Event) -> Future[None] | None:
        """Route an event to every matching trigger.

        Returns the Future of the last submitted run (handy for tests with a
        synchronous executor); ``None`` if nothing matched. An event the
        ``feed_gate`` rejects is dropped entirely (no card, no audit). Outside the
        working-hours rota, matching triggers are skipped (dropped, not queued)
        unless they set ``ignore_rota``.
        """
        if self._on_source_event is not None:
            try:
                self._on_source_event(event)
            except Exception:
                logger.exception("trigger source-event hook failed")
        if self._feed_gate is not None:
            try:
                if not self._feed_gate(event):
                    # Suppressed: the host folded it onto a queue card above, but
                    # it's not routed (e.g. an unclaimed help-desk thread) — no
                    # card, no run, no audit record, matching the chat claim gate.
                    return None
            except Exception:
                logger.exception("trigger feed-gate failed")  # fail open — route
        last: Future[None] | None = None
        clock_ok = self._clock_ok()
        # Lock auto-suspend is absolute (no ignore_rota bypass) and applies to
        # reactive events only — schedule-sourced ticks are pre-authorised jobs.
        lock_ok = self._lock_ok() or event.source == "schedule"
        matched: list[str] = []
        skipped: list[str] = []
        # Why each skipped trigger was gated (name → short reason), so the UI can
        # tell a lock skip from a rota skip without the operator opening the log.
        skip_reasons: dict[str, str] = {}
        deduped: list[str] = []
        for trigger in self._triggers:
            if not trigger.matches(event):
                continue
            if not lock_ok:
                skipped.append(trigger.name)
                skip_reasons[trigger.name] = "screen locked"
                logger.info("trigger '%s' skipped: session locked", trigger.name)
                continue
            if not clock_ok and not trigger.ignore_rota:
                skipped.append(trigger.name)
                skip_reasons[trigger.name] = "working hours"
                logger.info("trigger '%s' skipped: outside working hours", trigger.name)
                continue
            # once_per_thread: fire for the first message in a thread, skip later
            # replies. Resolve the trigger's tri-state against the mailbox default;
            # fail open when the event carries no thread id.
            once = (
                trigger.once_per_thread
                if trigger.once_per_thread is not None
                else self._default_once_per_thread
            )
            if once:
                key = thread_key(event.payload)
                if key and self._thread_store.already_fired(trigger.name, key):
                    deduped.append(trigger.name)
                    logger.info(
                        "trigger '%s' skipped: already fired for thread %s",
                        trigger.name,
                        key,
                    )
                    continue
            matched.append(trigger.name)
            last = self._dispatch(trigger, event)
        if not matched and not skipped and not deduped:
            logger.info("event %s:%s matched no triggers", event.source, event.type)
        self._record_event(event, matched, skipped, deduped, skip_reasons)
        return last

    def _record_event(
        self,
        event: Event,
        matched: list[str],
        skipped: list[str],
        deduped: list[str],
        skip_reasons: dict[str, str] | None = None,
    ) -> None:
        """Emit the per-event audit record (every event, with its disposition)."""
        if self._on_event is None:
            return
        try:
            self._on_event(
                {
                    "id": event.id,
                    "source": event.source,
                    "type": event.type,
                    "summary": _summarize(event),
                    "matched": matched,
                    "skipped": skipped,  # matched but gated (lock / rota)
                    "skip_reasons": skip_reasons or {},  # skipped name → why
                    "deduped": deduped,  # matched but a once_per_thread repeat
                    "ts": event.received_at or time.time(),
                }
            )
        except Exception:
            logger.exception("trigger event-audit hook failed")

    def _dispatch(
        self, trigger: Trigger, event: Event, *, claim_gate: bool = True
    ) -> Future[None] | None:
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
        # Claim gate: a claim-enabled trigger parks the card until an operator wins
        # the claim (bridge.claim_email → resume_claim). No action runs yet; the
        # card sits in AWAITING_CLAIM and is not submitted to the executor.
        if claim_gate and trigger.claim and card.id not in self._claim_granted:
            self._parked_claims[card.id] = (card, trigger, event)
            card.move(
                Column.AWAITING_CLAIM,
                claim=True,
                claim_folder=trigger.claim_folder,
                entry_id=str(event.payload.get("entry_id") or ""),
                mailbox=str(event.payload.get("mailbox") or ""),
                folder=str(event.payload.get("folder") or ""),
            )
            return None
        return self._executor.submit(self._run, card, trigger, event)

    def _run(self, card: TriggerCard, trigger: Trigger, event: Event) -> None:
        """Run one card's action, serialised against other runs of the same
        trigger, with a guaranteed terminal column (unless the handler parked it)."""
        lock = self._trigger_locks.setdefault(trigger.name, threading.Lock())
        with lock:
            try:
                self._handler(card, trigger, event)
                if not card.terminal and card.column not in _PARKED:
                    card.move(Column.DONE)
            except Exception as exc:  # noqa: BLE001 — surface on the card, don't crash the worker
                logger.exception("trigger '%s' card %s failed", trigger.name, card.id)
                card.move(Column.FAILED, error=str(exc))

    # -- claim gate (shared-mailbox coordination) --

    def parked_claim(self, card_id: str) -> tuple[Trigger, Event] | None:
        """The ``(trigger, event)`` of a card parked awaiting claim, or ``None``.
        The bridge reads the event payload (entry_id/mailbox/folder) + the
        trigger's ``claim_folder`` to attempt the Exchange move."""
        parked = self._parked_claims.get(card_id)
        return (parked[1], parked[2]) if parked is not None else None

    def parked_claim_ids(self) -> list[str]:
        """Card ids currently parked awaiting a claim (for the bridge's poll)."""
        return list(self._parked_claims)

    def resume_claim(self, card_id: str) -> bool:
        """A claim was won for ``card_id`` — run the trigger's action now. Marks the
        card granted and re-dispatches ``_run`` (which skips the gate). Returns
        ``False`` if the card isn't parked (already resolved / unknown)."""
        parked = self._parked_claims.pop(card_id, None)
        if parked is None:
            return False
        card, trigger, event = parked
        self._claim_granted.add(card_id)
        card.move(Column.TRIAGE, claimed=True)
        self._executor.submit(self._run, card, trigger, event)
        return True

    def mark_claimed_elsewhere(self, card_id: str, owner: str = "") -> bool:
        """The item was claimed on another console — resolve ``card_id`` as done,
        noting the owner. Returns ``False`` if the card isn't parked."""
        parked = self._parked_claims.pop(card_id, None)
        if parked is None:
            return False
        card, _trigger, _event = parked
        who = str(owner or "").strip()
        card.move(
            Column.DONE,
            claimed_elsewhere=True,
            claimed_by=who,
            note=f"claimed by {who}" if who else "claimed on another console",
        )
        return True


def load_triggers(
    configs: list[dict[str, Any]], *, ai_enabled: bool = True
) -> tuple[list[Trigger], list[str]]:
    """Parse a list of ``[triggers.<name>]`` tables.

    Returns ``(triggers, errors)``: a bad table is skipped with a message rather
    than sinking the whole set, so one typo doesn't disable every trigger.
    ``ai_enabled`` is forwarded to :meth:`Trigger.from_config`.
    """
    triggers: list[Trigger] = []
    errors: list[str] = []
    for raw in configs:
        try:
            triggers.append(Trigger.from_config(raw, ai_enabled=ai_enabled))
        except ValueError as exc:
            errors.append(str(exc))
    return triggers, errors

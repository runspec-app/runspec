"""
sources/schedule.py — the time-based (cron / interval) event source.

A *schedule* is configured in the **Schedules tab** (``runspec_schedules.toml``),
not as a trigger. When it fires it does one of four things, chosen by ``action``:

  - ``action = "event"`` → emit a rich, matchable :class:`~runspec_console.events.Event`
    into the trigger engine, exactly like a new mail arriving. Triggers with
    ``source = "schedule"`` pick it up by ``event`` type + ``match`` rules — so a
    *single* scheduled emit can fan out to many triggers, each with its own
    autonomy / agent-or-rule / match. The clock becomes just another event
    source, reusing the whole email mental model.

  - ``action = "runnable"`` → run a specific runnable on a host with fixed args,
    **directly and autonomously** — no agent, no confirm gate (scheduling it *is*
    the authorization), fully audited like any other run. The bridge supplies the
    run via the ``run_runnable`` callback.

  - ``action = "prompt"`` → run a headless **agent turn** with a fixed prompt, so
    the human (or the agent itself) can schedule something to *check or do later*
    in natural language — "every morning, check disk usage on the web hosts and
    flag anything over 80%". The agent has the same toolset and per-tool autonomy
    gate as a reactive trigger turn.

  - ``action = "runbook"`` → run a saved **runbook runbook** (by id) as an agent turn:
    the runbook body is rendered (with any ``values`` filling its ``{{placeholders}}``)
    and handed to the agent as its instructions. Schedule a reusable runbook to run
    on a cadence without re-typing it.

  - ``action = "saved_run"`` → fire a saved run (by id) — a deterministic preset
    (runnable + target + fixed args + open params) from ``runspec_saved_runs.toml``.
    Runs **directly and autonomously** like a runnable action (no agent, no
    confirm gate), with the schedule's ``values`` filling the preset's open
    params. The bridge supplies the run via the ``run_saved`` callback.

The ``prompt`` and ``runbook`` actions both run an agent turn via the bridge's
``run_agent`` callback. The source reads the schedule list, builds one APScheduler
job per *enabled* schedule, and on each fire calls the right path. It is
**bridge-owned and marked external** on the engine (it needs the schedule list and
the run paths, which the zero-arg registry factory can't supply) and is still
``register_source``'d so the Triggers UI lists "schedule" in the source dropdown.

APScheduler is an optional dependency (the ``schedule`` extra). The pure helpers
(``parse_when`` / ``parse_schedule_entry`` / ``_parse_every`` / ``event_payload``)
need none of it and are unit-tested directly; the ``apscheduler`` imports stay
lazy inside ``available()`` / ``_when_to_trigger`` / ``start``. See
docs/design/console-triggers.md.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..events import EmitFn, Event, register_source

logger = logging.getLogger("runspec.console.triggers.schedule")

# Mon..Sun, indexed by datetime.weekday() (Mon=0).
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# every = "<n><unit>" — unit → the IntervalTrigger keyword it maps to.
_INTERVAL_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

# What a runnable-action schedule calls to run its runnable:
# (schedule_id, host, runnable, args, group, hosts, selected_hosts, credential_ids).
# The id lets the bridge label the run card; ``group`` is non-empty for a
# group-targeted schedule (then ``host`` is "") and the bridge fans the run out
# across the group's members; ``hosts`` is a non-empty candidate list for an "any
# one of these" schedule (the bridge picks the first connected one at fire time);
# ``selected_hosts`` is a non-empty explicit list and the bridge fans the run out
# to every host in it (like ``group`` but without a registered group entry).
# ``credential_ids`` are console credentials applied to the run at fire time.
# The trailing ``command`` is the subcommand path to descend into (empty = root).
RunFn = Callable[
    [str, str, str, dict[str, Any], str, list[str], list[str], list[str], list[str]],
    None,
]

# What a prompt/runbook-action schedule calls to run a headless agent turn. The
# bridge gets the whole parsed job (it needs the prompt / runbook id + values to
# render the agent's first message) and runs it on its own thread.
AgentFn = Callable[["ScheduleJob"], None]

# What a saved_run-action schedule calls to fire the saved run. The bridge gets
# the whole parsed job (it needs the saved-run id + values) and resolves the
# preset's own targeting deterministically — same no-agent path as a runnable.
SavedFn = Callable[["ScheduleJob"], None]

# What an eval-action schedule calls to run the fixture against the live agent.
# The bridge gets the whole job (it needs eval_fixture + the run options).
EvalFn = Callable[["ScheduleJob"], None]

# Working-hours gate (no args → True/False): whether the rota currently permits a
# fire. None means "no gate" (always fire).
ClockFn = Callable[[], bool]

# Per-fire audit hook: a record (matching the trigger event-log shape) of a
# schedule fire and its disposition, for the Schedules-tab event log.
ScheduleEventFn = Callable[[dict[str, Any]], None]

# Optional attention cue when a schedule fires (a named sound id / bundled cue /
# token). Best-effort — the bridge swallows failures; None means "no cue hook".
SoundFn = Callable[[str], None]


# ── schedule spec (pure — no apscheduler) ─────────────────────────────────────


@dataclass(frozen=True)
class When:
    """The timing half of a schedule. Exactly one of cron/every is set."""

    cron: str = ""
    every: str = ""
    timezone: str = ""


@dataclass(frozen=True)
class ScheduleJob:
    """A parsed ``[[schedule]]`` entry from ``runspec_schedules.toml``.

    ``action`` is ``"event"`` (emit a matchable event), ``"runnable"`` (run a
    specific runnable directly), ``"prompt"`` (run an agent turn on a fixed
    prompt), ``"runbook"`` (run a saved runbook as an agent turn), or
    ``"saved_run"`` (fire a saved run — a deterministic preset — directly).
    The other-action fields are simply empty.
    """

    id: str
    when: When
    action: str = "runnable"
    enabled: bool = True
    # Fire even outside the working-hours rota (escape hatch, mirrors a trigger's
    # ignore_rota). Default off — a schedule is gated like a trigger.
    ignore_rota: bool = False
    # action == "event"
    event: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    # action == "runnable" — exactly one of host / group / hosts / selected_hosts
    # is set. ``group`` fans the run out across every member of the host group;
    # ``hosts`` is a candidate list and the bridge runs it on the first connected
    # one (failover); ``selected_hosts`` is an explicit list and the bridge fans
    # the run out to every host in it (ad-hoc group without a registered entry).
    host: str = ""
    group: str = ""
    hosts: list[str] = field(default_factory=list)
    selected_hosts: list[str] = field(default_factory=list)
    runnable: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    # action == "runnable" — the subcommand path to descend into (e.g. ["db",
    # "restore"]; empty = the root command). Mirrors a saved run's ``command``.
    command: list[str] = field(default_factory=list)
    # action == "runnable" — ids of console credentials to apply to the run. Threaded
    # to invoke_runnable exactly like the Forms tab's explicit selection: the secret
    # stays in the keychain and is injected at fire time (name-derived env vars, e.g.
    # JIRA_USERNAME/JIRA_PASSWORD, plus any username_arg/secret_arg bindings). Only
    # the ids are stored in runspec_schedules.toml — never a secret.
    credential_ids: list[str] = field(default_factory=list)
    # action == "prompt" — the fixed instruction handed to the agent turn.
    prompt: str = ""
    # action == "runbook" — the saved runbook id, plus values for its {{placeholders}}.
    # ``values`` is shared with action == "saved_run" (there it fills the saved
    # run's open params).
    runbook: str = ""
    values: dict[str, Any] = field(default_factory=dict)
    # action == "saved_run" — the saved-run (preset) id from runspec_saved_runs.toml.
    saved_run: str = ""
    # action == "eval" — the eval fixture (suite name) to run against the live
    # agent, plus the run options (samples per case / preflight-validate / emit an
    # OTLP trace per case). A dry run — it never executes a runnable.
    eval_fixture: str = ""
    eval_samples: int = 1
    eval_validate: bool = False
    eval_telemetry: bool = False
    # Optional audio cue played when this schedule fires (any action) — a named
    # sound id, a bundled cue name, or a full token. Best-effort + Windows-only;
    # never affects whether the action runs. Resolved via sounds.play_identifier.
    sound: str = ""


def _parse_every(every: str) -> dict[str, int]:
    """``"15m"`` → ``{"minutes": 15}``. Raises ``ValueError`` on a bad interval.

    A single positive integer followed by a unit suffix (``s``/``m``/``h``/``d``)
    — the IntervalTrigger keyword args. Kept deliberately small; cron covers
    anything more elaborate.
    """
    text = str(every).strip().lower()
    if len(text) < 2 or text[-1] not in _INTERVAL_UNITS:
        raise ValueError(
            f"schedule 'every' must be a number followed by s/m/h/d (got {every!r}), "
            'e.g. every = "15m"'
        )
    try:
        n = int(text[:-1])
    except ValueError:
        raise ValueError(
            f"schedule 'every' must start with a whole number (got {every!r})"
        ) from None
    if n <= 0:
        raise ValueError(f"schedule 'every' must be positive (got {every!r})")
    return {_INTERVAL_UNITS[text[-1]]: n}


def parse_when(table: dict[str, Any]) -> When:
    """Validate and parse a schedule's ``when`` table.

    Requires **exactly one** of ``cron`` / ``every`` (an ``every`` value is also
    syntax-checked here so a typo surfaces at config-load time, not at fire time).
    ``timezone`` is optional and defaults to the PC-local zone.
    """
    cron = str(table.get("cron") or "").strip()
    every = str(table.get("every") or "").strip()
    timezone = str(table.get("timezone") or "").strip()
    if bool(cron) == bool(every):
        raise ValueError(
            "schedule needs exactly one of 'cron' or 'every' "
            '(e.g. cron = "30 17 * * mon-fri"  or  every = "15m")'
        )
    if every:
        _parse_every(every)  # validate now; raise with a human message
    return When(cron=cron, every=every, timezone=timezone)


# The schedule actions that run an agent turn (or drive the model) — refused
# when the console's AI off-switch (`[console] ai = false`) is set.
AI_SCHEDULE_ACTIONS: frozenset[str] = frozenset({"prompt", "runbook", "eval"})


def parse_schedule_entry(
    data: dict[str, Any], *, ai_enabled: bool = True
) -> ScheduleJob:
    """Validate and parse one ``[[schedule]]`` table into a :class:`ScheduleJob`.

    Raises ``ValueError`` with a human-first message on bad config. The ``when``
    block is required; ``action`` defaults to ``"runnable"``. An ``"event"``
    schedule needs an ``event`` name; a ``"runnable"`` schedule needs ``runnable``
    plus exactly one of ``host`` (one host) or ``group`` (fan out to a host group);
    a ``"prompt"`` schedule needs a ``prompt``; a ``"runbook"`` schedule needs a
    ``runbook`` id. With ``ai_enabled=False`` the ``prompt`` / ``runbook`` /
    ``eval`` actions are refused (they run an agent turn) — the job is reported
    invalid rather than silently dropped.
    """
    sid = str(data.get("id") or "").strip()
    if not sid:
        raise ValueError("schedule is missing an 'id'")

    action = str(data.get("action") or "runnable").strip()
    # Back-compat: runbooks were called "skills" before the rename. Accept a legacy
    # action="skill" schedule and treat it as a runbook.
    if action == "skill":
        action = "runbook"
    if action not in ("event", "runnable", "prompt", "runbook", "saved_run", "eval"):
        raise ValueError(
            f"schedule '{sid}' has unknown action '{action}' "
            "(expected 'event', 'runnable', 'prompt', 'runbook', 'saved_run' or 'eval')"
        )
    if not ai_enabled and action in AI_SCHEDULE_ACTIONS:
        raise ValueError(
            f"schedule '{sid}' has action '{action}', which runs an agent turn, "
            "but AI features are disabled in this console ([console] ai = false)"
        )

    when = parse_when(data.get("when") or {})

    event = ""
    fields: dict[str, str] = {}
    host = ""
    group = ""
    hosts: list[str] = []
    selected_hosts: list[str] = []
    runnable = ""
    args: dict[str, Any] = {}
    command: list[str] = []
    credential_ids: list[str] = []
    prompt = ""
    runbook = ""
    values: dict[str, Any] = {}
    saved_run = ""
    eval_fixture = ""
    eval_samples = 1
    eval_validate = False
    eval_telemetry = False
    if action == "event":
        event = str(data.get("event") or "").strip()
        if not event:
            raise ValueError(
                f"schedule '{sid}' uses action='event' but has no 'event' name"
            )
        fields = {str(k): str(v) for k, v in (data.get("fields") or {}).items()}
    elif action == "runnable":
        host = str(data.get("host") or "").strip()
        group = str(data.get("group") or "").strip()
        hosts = [str(h).strip() for h in (data.get("hosts") or []) if str(h).strip()]
        selected_hosts = [
            str(h).strip() for h in (data.get("selected_hosts") or []) if str(h).strip()
        ]
        runnable = str(data.get("runnable") or "").strip()
        if not runnable:
            raise ValueError(
                f"schedule '{sid}' uses action='runnable' but is missing 'runnable'"
            )
        # Exactly one targeting mode: a single host, a registered group (fan out to
        # every member), a candidate list (run on the first connected one), or an
        # explicit selected list (fan out to every host in it).
        modes = sum((bool(host), bool(group), bool(hosts), bool(selected_hosts)))
        if modes != 1:
            raise ValueError(
                f"schedule '{sid}' (action='runnable') needs exactly one of 'host' "
                "(a single host), 'group' (a host group to fan out across), 'hosts' "
                "(a candidate list to run on the first connected one), or "
                "'selected_hosts' (an explicit list to run on all of them)"
            )
        args = dict(data.get("args") or {})
        # Subcommand path — accept a bare string ("db restore") or a list,
        # exactly like a saved run's command.
        command_raw = data.get("command") or []
        if isinstance(command_raw, str):
            command = command_raw.split()
        else:
            command = [str(c).strip() for c in command_raw if str(c).strip()]
        credential_ids = [
            str(c).strip() for c in (data.get("credential_ids") or []) if str(c).strip()
        ]
    elif action == "prompt":
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(
                f"schedule '{sid}' uses action='prompt' but has no 'prompt' text"
            )
    elif action == "saved_run":
        saved_run = str(data.get("saved_run") or "").strip()
        if not saved_run:
            raise ValueError(
                f"schedule '{sid}' uses action='saved_run' but has no 'saved_run' id"
            )
        values = dict(data.get("values") or {})
    elif action == "eval":
        eval_fixture = str(data.get("eval_fixture") or "").strip()
        if not eval_fixture:
            raise ValueError(
                f"schedule '{sid}' uses action='eval' but has no 'eval_fixture'"
            )
        eval_samples = max(1, int(data.get("eval_samples") or 1))
        eval_validate = bool(data.get("eval_validate", False))
        eval_telemetry = bool(data.get("eval_telemetry", False))
    else:  # runbook
        # Read the new `runbook` key, falling back to the legacy `skill` key.
        runbook = str(data.get("runbook") or data.get("skill") or "").strip()
        if not runbook:
            raise ValueError(
                f"schedule '{sid}' uses action='runbook' but has no 'runbook' id"
            )
        values = dict(data.get("values") or {})

    return ScheduleJob(
        id=sid,
        when=when,
        action=action,
        enabled=bool(data.get("enabled", True)),
        ignore_rota=bool(data.get("ignore_rota", False)),
        event=event,
        fields=fields,
        host=host,
        group=group,
        hosts=hosts,
        selected_hosts=selected_hosts,
        runnable=runnable,
        args=args,
        command=command,
        credential_ids=credential_ids,
        prompt=prompt,
        runbook=runbook,
        values=values,
        saved_run=saved_run,
        eval_fixture=eval_fixture,
        eval_samples=eval_samples,
        eval_validate=eval_validate,
        eval_telemetry=eval_telemetry,
        sound=str(data.get("sound") or "").strip(),
    )


def _when_to_trigger(when: When) -> Any:
    """Build the APScheduler trigger for a :class:`When` (lazy apscheduler)."""
    tz = when.timezone or None
    if when.cron:
        from apscheduler.triggers.cron import CronTrigger

        return CronTrigger.from_crontab(when.cron, timezone=tz)
    from apscheduler.triggers.interval import IntervalTrigger

    return IntervalTrigger(timezone=tz, **_parse_every(when.every))


def next_fire_time(when: When, now: datetime | None = None) -> datetime | None:
    """The next time a :class:`When` would fire, as a timezone-aware datetime.

    Used by the Schedules tab to show a real "Next run" instead of "—". Lazily
    imports APScheduler (the ``schedule`` extra) and returns ``None`` when it is
    not installed or the timing can't be resolved, so callers can degrade
    gracefully. ``now`` defaults to the current time in the trigger's timezone.
    """
    try:
        trigger = _when_to_trigger(when)
    except Exception:
        return None
    tz = getattr(trigger, "timezone", None)
    if now is None:
        now = datetime.now(tz) if tz is not None else datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone(tz) if tz is not None else now.astimezone()
    try:
        return trigger.get_next_fire_time(None, now)
    except Exception:
        return None


def event_payload(job: ScheduleJob, now: datetime) -> dict[str, str]:
    """The payload for an ``action="event"`` schedule firing at local time ``now``.

    Built-in time fields (matchable / templatable, e.g. ``weekday``, ``hour``)
    plus the schedule id and event name, then the user's own ``fields`` overlaid
    on top so an explicit field always wins. Triggers ``match`` these the same way
    they match a mail subject.
    """
    payload = {
        "schedule": job.id,
        "event": job.event,
        "fired_at": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": _WEEKDAYS[now.weekday()],
        "dow": str(now.weekday()),
        "hour": now.strftime("%H"),
        "minute": now.strftime("%M"),
        "day": now.strftime("%d"),
        "month": now.strftime("%m"),
    }
    payload.update(job.fields)
    return payload


# ── the source ────────────────────────────────────────────────────────────────


class ScheduleSource:
    """Drives APScheduler from the configured schedules. Satisfies ``EventSource``.

    Constructed zero-arg for the source registry (UI listing only — no jobs); the
    bridge builds the live instance with the schedule list (and the ``run_runnable``
    callback for direct runs) and starts it, feeding event-action fires into
    ``engine.feed``.
    """

    id = "schedule"

    def __init__(
        self,
        schedules: list[dict[str, Any]] | None = None,
        *,
        ai_enabled: bool = True,
        run_runnable: RunFn | None = None,
        run_agent: AgentFn | None = None,
        run_saved: SavedFn | None = None,
        run_eval: EvalFn | None = None,
        clock_ok: ClockFn | None = None,
        on_event: ScheduleEventFn | None = None,
        play_sound: SoundFn | None = None,
    ) -> None:
        self._jobs: list[ScheduleJob] = []
        for raw in schedules or []:
            try:
                job = parse_schedule_entry(raw, ai_enabled=ai_enabled)
            except Exception:
                logger.exception(
                    "schedule: skipping invalid schedule %r",
                    (raw or {}).get("id", "?"),
                )
                continue
            if job.enabled:
                self._jobs.append(job)
        self._run_runnable = run_runnable
        # Prompt/runbook schedules run a headless agent turn through this callback.
        self._run_agent = run_agent
        # Saved-run schedules fire the preset deterministically through this one.
        self._run_saved = run_saved
        # Eval-action schedules run a fixture against the live agent (dry run).
        self._run_eval = run_eval
        # Working-hours gate + per-fire audit hook (both bridge-owned). When
        # clock_ok is set and returns False, a fire is skipped (unless the job sets
        # ignore_rota) — so schedules honour the same rota as triggers.
        self._clock_ok = clock_ok
        self._on_event = on_event
        # Best-effort attention cue when a job fires (after the rota gate passes).
        self._play_sound = play_sound
        self._emit: EmitFn | None = None
        self._scheduler: Any | None = None

    def available(self) -> tuple[bool, str]:
        try:
            import apscheduler  # noqa: F401
        except Exception:
            return (
                False,
                "APScheduler not installed — pip install runspec-console[schedule]",
            )
        return (True, "")

    def event_types(self) -> list[str]:
        """The distinct event names this machine's schedules emit (for the
        Triggers UI autocomplete). Empty for the zero-arg registry instance."""
        return sorted({j.event for j in self._jobs if j.action == "event" and j.event})

    def start(self, emit: EmitFn) -> None:
        ok, reason = self.available()
        if not ok:
            logger.info("schedule source not started: %s", reason)
            return
        if self._scheduler is not None:
            return
        self._emit = emit
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler()
        for job in self._jobs:
            try:
                scheduler.add_job(
                    self._fire,
                    _when_to_trigger(job.when),
                    args=[job.id],
                    id=job.id,
                    replace_existing=True,
                )
            except Exception:
                logger.exception("schedule: could not add job %r", job.id)
        scheduler.start()
        self._scheduler = scheduler
        logger.info("schedule source: %d job(s) running", len(scheduler.get_jobs()))

    def stop(self) -> None:
        scheduler, self._scheduler = self._scheduler, None
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                logger.exception("schedule: error shutting down scheduler")
        self._emit = None

    def _fire(self, job_id: str) -> None:
        """APScheduler job callback: rota-gate, then emit the event or run the
        runnable, recording the disposition for the Schedules event log."""
        job = next((j for j in self._jobs if j.id == job_id), None)
        if job is None:
            return
        # Working-hours gate: outside the rota a schedule is skipped (dropped, not
        # queued), exactly like a trigger — unless it opts out with ignore_rota.
        if self._clock_ok is not None and not job.ignore_rota:
            try:
                clock_ok = self._clock_ok()
            except Exception:
                logger.exception("schedule: rota gate failed — firing %r", job_id)
                clock_ok = True  # fail open
            if not clock_ok:
                logger.info("schedule '%s' skipped: outside working hours", job.id)
                self._record(job, skipped=True)
                return
        # Attention cue as the job fires (after the rota gate, before the action).
        # Best-effort: a failing cue must not stop the schedule from running.
        if job.sound and self._play_sound is not None:
            try:
                self._play_sound(job.sound)
            except Exception:
                logger.debug("schedule: sound cue failed for %r", job_id, exc_info=True)
        try:
            if job.action == "event":
                if self._emit is not None:
                    self._emit(
                        Event.new(
                            "schedule", job.event, event_payload(job, datetime.now())
                        )
                    )
            elif job.action in ("prompt", "runbook"):
                if self._run_agent is not None:
                    self._run_agent(job)
            elif job.action == "saved_run":
                if self._run_saved is not None:
                    self._run_saved(job)
            elif job.action == "eval":
                if self._run_eval is not None:
                    self._run_eval(job)
            elif self._run_runnable is not None:
                self._run_runnable(
                    job.id,
                    job.host,
                    job.runnable,
                    dict(job.args),
                    job.group,
                    list(job.hosts),
                    list(job.selected_hosts),
                    list(job.credential_ids),
                    list(job.command),
                )
            self._record(job, skipped=False)
        except Exception:
            logger.exception("schedule: fire failed for %r", job_id)

    def _record(self, job: ScheduleJob, *, skipped: bool) -> None:
        """Push a Schedules event-log record (trigger-event shape) for one fire.

        ``matched``/``skipped`` carry the schedule id so the reused Event-log
        widget renders "fired …" / "skipped (working hours) …" with no changes."""
        if self._on_event is None:
            return
        if job.action == "event":
            etype, summary = job.event, f"emit {job.event}"
        elif job.action == "prompt":
            etype = "prompt"
            summary = f"prompt: {job.prompt[:60]}"
        elif job.action == "runbook":
            etype = "runbook"
            summary = f"runbook: {job.runbook}"
        elif job.action == "saved_run":
            etype = "saved_run"
            summary = f"saved run: {job.saved_run}"
        elif job.action == "eval":
            etype = "eval"
            summary = f"eval: {job.eval_fixture}"
        else:
            etype = job.runnable or "runnable"
            if job.group:
                where = f"group:{job.group}"
            elif job.selected_hosts:
                where = f"selected:{','.join(job.selected_hosts)}"
            elif job.hosts:
                where = f"any:{','.join(job.hosts)}"
            else:
                where = job.host
            summary = f"/{job.runnable} on {where}"
        try:
            self._on_event(
                {
                    "id": uuid.uuid4().hex[:12],
                    "source": "schedule",
                    "type": etype,
                    "summary": summary,
                    "matched": [] if skipped else [job.id],
                    "skipped": [job.id] if skipped else [],
                    "ts": time.time(),
                }
            )
        except Exception:
            logger.exception("schedule event-audit hook failed")


register_source("schedule", ScheduleSource)

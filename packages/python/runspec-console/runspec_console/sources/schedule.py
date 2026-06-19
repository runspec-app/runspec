"""
sources/schedule.py — the time-based (cron / interval) event source.

A *schedule* is configured in the **Schedules tab** (``runspec_schedules.toml``),
not as a trigger. When it fires it does one of two things, chosen by ``action``:

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

The source reads the schedule list, builds one APScheduler job per *enabled*
schedule, and on each fire calls the right path. It is **bridge-owned and marked
external** on the engine (it needs the schedule list and the run path, which the
zero-arg registry factory can't supply) and is still ``register_source``'d so the
Triggers UI lists "schedule" in the source dropdown.

APScheduler is an optional dependency (the ``schedule`` extra). The pure helpers
(``parse_when`` / ``parse_schedule_entry`` / ``_parse_every`` / ``event_payload``)
need none of it and are unit-tested directly; the ``apscheduler`` imports stay
lazy inside ``available()`` / ``_when_to_trigger`` / ``start``. See
docs/design/console-triggers.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..events import EmitFn, Event, register_source

logger = logging.getLogger("runspec.console.triggers.schedule")

# Mon..Sun, indexed by datetime.weekday() (Mon=0).
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# every = "<n><unit>" — unit → the IntervalTrigger keyword it maps to.
_INTERVAL_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

# What a runnable-action schedule calls to run its runnable: (host, runnable, args).
RunFn = Callable[[str, str, dict[str, Any]], None]


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

    ``action`` is ``"event"`` (emit a matchable event) or ``"runnable"`` (run a
    specific runnable directly). The other-action fields are simply empty.
    """

    id: str
    when: When
    action: str = "runnable"
    enabled: bool = True
    # action == "event"
    event: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    # action == "runnable"
    host: str = ""
    runnable: str = ""
    args: dict[str, Any] = field(default_factory=dict)


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


def parse_schedule_entry(data: dict[str, Any]) -> ScheduleJob:
    """Validate and parse one ``[[schedule]]`` table into a :class:`ScheduleJob`.

    Raises ``ValueError`` with a human-first message on bad config. The ``when``
    block is required; ``action`` defaults to ``"runnable"``. An ``"event"``
    schedule needs an ``event`` name; a ``"runnable"`` schedule needs ``host`` and
    ``runnable``.
    """
    sid = str(data.get("id") or "").strip()
    if not sid:
        raise ValueError("schedule is missing an 'id'")

    action = str(data.get("action") or "runnable").strip()
    if action not in ("event", "runnable"):
        raise ValueError(
            f"schedule '{sid}' has unknown action '{action}' "
            "(expected 'event' or 'runnable')"
        )

    when = parse_when(data.get("when") or {})

    event = ""
    fields: dict[str, str] = {}
    host = ""
    runnable = ""
    args: dict[str, Any] = {}
    if action == "event":
        event = str(data.get("event") or "").strip()
        if not event:
            raise ValueError(
                f"schedule '{sid}' uses action='event' but has no 'event' name"
            )
        fields = {str(k): str(v) for k, v in (data.get("fields") or {}).items()}
    else:  # runnable
        host = str(data.get("host") or "").strip()
        runnable = str(data.get("runnable") or "").strip()
        if not host or not runnable:
            raise ValueError(
                f"schedule '{sid}' uses action='runnable' but is missing "
                "'host' and/or 'runnable'"
            )
        args = dict(data.get("args") or {})

    return ScheduleJob(
        id=sid,
        when=when,
        action=action,
        enabled=bool(data.get("enabled", True)),
        event=event,
        fields=fields,
        host=host,
        runnable=runnable,
        args=args,
    )


def _when_to_trigger(when: When) -> Any:
    """Build the APScheduler trigger for a :class:`When` (lazy apscheduler)."""
    tz = when.timezone or None
    if when.cron:
        from apscheduler.triggers.cron import CronTrigger

        return CronTrigger.from_crontab(when.cron, timezone=tz)
    from apscheduler.triggers.interval import IntervalTrigger

    return IntervalTrigger(timezone=tz, **_parse_every(when.every))


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
        run_runnable: RunFn | None = None,
    ) -> None:
        self._jobs: list[ScheduleJob] = []
        for raw in schedules or []:
            try:
                job = parse_schedule_entry(raw)
            except Exception:
                logger.exception(
                    "schedule: skipping invalid schedule %r",
                    (raw or {}).get("id", "?"),
                )
                continue
            if job.enabled:
                self._jobs.append(job)
        self._run_runnable = run_runnable
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
        """APScheduler job callback: emit the event, or run the runnable."""
        job = next((j for j in self._jobs if j.id == job_id), None)
        if job is None:
            return
        try:
            if job.action == "event":
                if self._emit is not None:
                    self._emit(
                        Event.new(
                            "schedule", job.event, event_payload(job, datetime.now())
                        )
                    )
            elif self._run_runnable is not None:
                self._run_runnable(job.host, job.runnable, dict(job.args))
        except Exception:
            logger.exception("schedule: fire failed for %r", job_id)


register_source("schedule", ScheduleSource)

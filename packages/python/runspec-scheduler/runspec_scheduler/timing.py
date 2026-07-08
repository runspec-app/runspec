"""timing.py — cron/interval timing, mirrored from runspec-console for parity.

This is a deliberate, small **duplicate** of the pure timing helpers in
``runspec_console.sources.schedule`` (``When`` / ``_parse_every`` / ``parse_when``
/ ``_when_to_trigger`` / ``next_fire_time``). The scheduler must fire a delegated
schedule *identically* to the way the console would have fired it, so the two
implementations are kept byte-for-byte equivalent in behaviour and pinned by
``tests/test_timing_parity.py`` (which imports both and asserts identical
``next_fire_time`` across a cron/interval/timezone matrix).

We duplicate rather than share a module because there is no home for shared code:
runspec-console is a heavy dependency we must not take, and core ``runspec`` is
stdlib-only/zero-dep so the APScheduler-touching trigger builder can't live there.
This is the same "Node mirrors Python" parity pattern the repo already uses.

APScheduler is a hard dependency of this package, so (unlike the console) the
imports here need not be lazy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# every = "<n><unit>" — unit → the IntervalTrigger keyword it maps to.
_INTERVAL_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


@dataclass(frozen=True)
class When:
    """The timing half of a schedule. Exactly one of cron/every is set."""

    cron: str = ""
    every: str = ""
    timezone: str = ""


def _parse_every(every: str) -> dict[str, int]:
    """``"15m"`` → ``{"minutes": 15}``. Raises ``ValueError`` on a bad interval.

    A single positive integer followed by a unit suffix (``s``/``m``/``h``/``d``)
    — the IntervalTrigger keyword args. Kept deliberately small; cron covers
    anything more elaborate.
    """
    text = str(every).strip().lower()
    if len(text) < 2 or text[-1] not in _INTERVAL_UNITS:
        raise ValueError(f"schedule 'every' must be a number followed by s/m/h/d (got {every!r}), e.g. every = \"15m\"")
    try:
        n = int(text[:-1])
    except ValueError:
        raise ValueError(f"schedule 'every' must start with a whole number (got {every!r})") from None
    if n <= 0:
        raise ValueError(f"schedule 'every' must be positive (got {every!r})")
    return {_INTERVAL_UNITS[text[-1]]: n}


def parse_when(table: dict[str, Any]) -> When:
    """Validate and parse a schedule's ``when`` table.

    Requires **exactly one** of ``cron`` / ``every`` (an ``every`` value is also
    syntax-checked here so a typo surfaces at config-load time, not at fire time).
    ``timezone`` is optional and defaults to the host-local zone.
    """
    cron = str(table.get("cron") or "").strip()
    every = str(table.get("every") or "").strip()
    timezone = str(table.get("timezone") or "").strip()
    if bool(cron) == bool(every):
        raise ValueError("schedule needs exactly one of 'cron' or 'every' (e.g. cron = \"30 17 * * mon-fri\"  or  every = \"15m\")")
    if every:
        _parse_every(every)  # validate now; raise with a human message
    return When(cron=cron, every=every, timezone=timezone)


def when_to_trigger(when: When) -> Any:
    """Build the APScheduler trigger for a :class:`When`."""
    tz = when.timezone or None
    if when.cron:
        from apscheduler.triggers.cron import CronTrigger

        return CronTrigger.from_crontab(when.cron, timezone=tz)
    from apscheduler.triggers.interval import IntervalTrigger

    return IntervalTrigger(timezone=tz, **_parse_every(when.every))


def next_fire_time(when: When, now: datetime | None = None) -> datetime | None:
    """The next time a :class:`When` would fire, as a timezone-aware datetime.

    Lazily builds the trigger and returns ``None`` when the timing can't be
    resolved, so callers can degrade gracefully. ``now`` defaults to the current
    time in the trigger's timezone.
    """
    try:
        trigger = when_to_trigger(when)
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

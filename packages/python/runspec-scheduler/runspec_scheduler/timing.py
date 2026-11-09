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
from datetime import datetime, timedelta, timezone
from functools import lru_cache
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


#: cron's ``@`` nicknames, as 5-field expressions. APScheduler's ``from_crontab``
#: takes only 5 fields, so a nickname is expanded before it gets there (cron
#: itself reads them as they are). ``@reboot`` isn't a time, so it has none.
CRON_NICKNAMES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


def expand_cron(expr: str) -> str:
    """A cron expression with an ``@nickname`` expanded to its 5 fields.

    Anything else is returned unchanged. Raises ``ValueError`` for an unknown
    nickname (``@reboot`` included: it isn't a time of day)."""
    text = str(expr).strip()
    if not text.startswith("@"):
        return text
    fields = CRON_NICKNAMES.get(text.lower())
    if fields is None:
        known = ", ".join(sorted(CRON_NICKNAMES))
        raise ValueError(f"schedule 'cron' nickname {text!r} isn't a time (use one of {known}, or 5 fields)")
    return fields


def _fixed_time(fields: str) -> bool:
    """Whether a 5-field expression names fixed times of day (no ``*`` in its
    minute or hour). cron runs such a job once when the clocks go back; a job
    with ``*`` there runs by elapsed time, so it runs in both copies of the hour."""
    parts = fields.split()
    return len(parts) == 5 and "*" not in parts[0] and "*" not in parts[1]


def later(moment: datetime, delta: timedelta) -> datetime:
    """``moment + delta`` counted in real time. Adding to a zoned datetime adds
    to its wall clock and drops ``fold``, so in the hour the clocks go back,
    01:30 GMT + 1s comes out as 01:30:01 BST, an hour *earlier*."""
    return moment.astimezone(timezone.utc) + delta


def _wall(moment: datetime, tz: Any) -> datetime:
    """``moment`` as a naive wall-clock time in ``tz`` (the clock on the wall)."""
    return moment.astimezone(tz).replace(tzinfo=None)


_ONCE_CRON_CLASS: Any = None


def _once_cron_class() -> Any:
    """A ``CronTrigger`` that fires a fixed-time schedule once when the clocks go
    back, as cron does. APScheduler 3 fires ``30 1 * * *`` in Europe/London at
    01:30 BST and again at 01:30 GMT on the October change; this skips a fire whose
    wall-clock time isn't later than the previous fire's. (Built on first use so
    APScheduler is imported lazily.)"""
    global _ONCE_CRON_CLASS
    if _ONCE_CRON_CLASS is None:
        from apscheduler.triggers.cron import CronTrigger

        class OnceCronTrigger(CronTrigger):  # type: ignore[misc]
            def get_next_fire_time(self, previous_fire_time: Any, now: Any) -> Any:
                nxt = super().get_next_fire_time(previous_fire_time, now)
                if previous_fire_time is None:
                    return nxt
                prev_wall = _wall(previous_fire_time, self.timezone)
                for _ in range(8):  # a repeated hour holds at most a few fires
                    if nxt is None or _wall(nxt, self.timezone) > prev_wall:
                        return nxt
                    nxt = super().get_next_fire_time(nxt, later(nxt, timedelta(seconds=1)))
                return nxt

        _ONCE_CRON_CLASS = OnceCronTrigger
    return _ONCE_CRON_CLASS


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
    if cron:
        expand_cron(cron)  # an unknown @nickname fails now, not at fire time
    return When(cron=cron, every=every, timezone=timezone)


def when_to_trigger(when: When) -> Any:
    """Build the APScheduler trigger for a :class:`When`."""
    tz = when.timezone or None
    if when.cron:
        from apscheduler.triggers.cron import CronTrigger

        fields = expand_cron(when.cron)
        cls = _once_cron_class() if _fixed_time(fields) else CronTrigger
        return cls.from_crontab(fields, timezone=tz)
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


# ── the host's zone (the cron backend fires on it) ──────────────────────────

_UTC_NAMES = frozenset(
    n.lower()
    for n in (
        "UTC", "UCT", "Universal", "Zulu", "GMT", "GMT0", "GMT+0", "GMT-0", "Greenwich",
        "Etc/UTC", "Etc/UCT", "Etc/Universal", "Etc/Zulu", "Etc/GMT", "Etc/GMT0", "Etc/GMT+0", "Etc/GMT-0", "Etc/Greenwich",
    )
)  # fmt: skip


def host_zone_name() -> str:
    """The IANA name of this host's local zone ("" when it can't be told).

    Uses tzlocal (an APScheduler dependency), which reads ``$TZ`` and then the
    system setting (``/etc/localtime``, ``timedatectl``)."""
    try:
        import tzlocal

        return str(tzlocal.get_localzone_name() or "")
    except Exception:  # noqa: BLE001 — an unreadable zone is reported, not raised
        return ""


@lru_cache(maxsize=256)
def same_zone(a: str, b: str) -> bool:
    """Whether two zone names keep the same clock: equal names, two names for
    UTC, or two names (aliases like Europe/Belfast and Europe/London) whose UTC
    offset matches at every hour for two years. ``False`` if either is unknown."""
    if not a or not b:
        return False
    if a == b or (a.lower() in _UTC_NAMES and b.lower() in _UTC_NAMES):
        return True
    from zoneinfo import ZoneInfo

    try:
        za, zb = ZoneInfo(a), ZoneInfo(b)
    except Exception:  # noqa: BLE001 — an unknown name never matches
        return False
    start = datetime(datetime.now(timezone.utc).year, 1, 1, tzinfo=timezone.utc)
    for hour in range(0, 2 * 366 * 24):
        moment = start + timedelta(hours=hour)
        if moment.astimezone(za).utcoffset() != moment.astimezone(zb).utcoffset():
            return False
    return True


def cron_zone_problem(timezone: str, host_zone: str | None = None) -> str:
    """Why a schedule in ``timezone`` can't be a crontab line here, or "".

    A crontab line fires on the host's clock and has no zone of its own (cronie's
    ``CRON_TZ`` would also move every line after it, and Debian's cron ignores
    it), so a schedule in another zone needs the daemon backend."""
    if not timezone:
        return ""
    host = host_zone_name() if host_zone is None else host_zone
    if same_zone(timezone, host):
        return ""
    here = f"this host is on {host}" if host else "this host's zone can't be told"
    return f"timezone {timezone}: {here}, and a crontab line fires on the host's clock — this schedule needs the daemon backend (runspec-scheduler use daemon), or leave its timezone blank"

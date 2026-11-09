"""Timing tests: concrete oracle assertions + parity with runspec-console."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from runspec_scheduler.timing import (
    When,
    _parse_every,
    cron_zone_problem,
    expand_cron,
    later,
    next_fire_time,
    parse_when,
    same_zone,
    when_to_trigger,
)


def test_parse_every_units():
    assert _parse_every("15m") == {"minutes": 15}
    assert _parse_every("2h") == {"hours": 2}
    assert _parse_every("1d") == {"days": 1}
    assert _parse_every("30s") == {"seconds": 30}


@pytest.mark.parametrize("bad", ["", "m", "0m", "-5m", "5x", "abc"])
def test_parse_every_rejects(bad):
    with pytest.raises(ValueError):
        _parse_every(bad)


def test_parse_when_requires_exactly_one():
    with pytest.raises(ValueError):
        parse_when({})
    with pytest.raises(ValueError):
        parse_when({"cron": "0 2 * * *", "every": "15m"})
    assert parse_when({"cron": "0 2 * * *"}).cron == "0 2 * * *"
    assert parse_when({"every": "15m"}).every == "15m"


def test_next_fire_interval_oracle():
    # A far-future `now` so it's always past the trigger's construction-time
    # start_date (APScheduler's default) regardless of the machine's wall clock —
    # then the next fire is deterministically within one interval of `now`.
    now = datetime(3000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    nxt = next_fire_time(When(every="15m", timezone="UTC"), now=now)
    assert nxt is not None
    delta = (nxt - now).total_seconds()
    assert 0 < delta <= 15 * 60 + 1


def test_next_fire_cron_oracle():
    now = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
    nxt = next_fire_time(When(cron="0 2 * * *", timezone="UTC"), now=now)
    assert nxt is not None
    # Next 02:00 UTC is the following day.
    assert (nxt.year, nxt.month, nxt.day, nxt.hour, nxt.minute) == (2026, 7, 9, 2, 0)


def test_parity_with_console():
    """Anti-drift: our timing must match runspec-console's.

    Cron triggers are deterministic and compared exactly. Interval triggers
    align to the trigger's *construction* time (APScheduler's default
    ``start_date``), so two independent constructions differ by microseconds —
    identical semantics, compared within a small tolerance.
    """
    console = pytest.importorskip("runspec_console.sources.schedule")
    now = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
    cron_cases = [
        {"cron": "0 2 * * *"},
        {"cron": "30 17 * * mon-fri"},
        {"cron": "*/5 * * * *", "timezone": "Europe/London"},
        {"cron": "@daily"},
        {"cron": "30 1 * * *", "timezone": "Europe/London"},
    ]
    for c in cron_cases:
        ours = next_fire_time(When(**c), now=now)
        theirs = console.next_fire_time(console.When(**c), now=now)
        assert ours == theirs, c

    interval_cases = [
        {"every": "15m"},
        {"every": "2h", "timezone": "America/New_York"},
        {"every": "1d"},
    ]
    for c in interval_cases:
        ours = next_fire_time(When(**c), now=now)
        theirs = console.next_fire_time(console.When(**c), now=now)
        assert ours is not None and theirs is not None, c
        assert abs((ours - theirs).total_seconds()) < 2, c


# ── nicknames, the repeated hour, the host's zone (0.8.0) ───────────────────


@pytest.mark.parametrize(
    ("nick", "fields"),
    [("@daily", "0 0 * * *"), ("@midnight", "0 0 * * *"), ("@hourly", "0 * * * *"), ("@weekly", "0 0 * * 0"), ("@monthly", "0 0 1 * *"), ("@YEARLY", "0 0 1 1 *")],
)
def test_expand_cron_nicknames(nick, fields):
    assert expand_cron(nick) == fields


def test_nickname_fires_on_the_daemon():
    # APScheduler's from_crontab alone rejects '@daily' (needs 5 fields).
    now = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
    nxt = next_fire_time(When(cron="@daily", timezone="UTC"), now=now)
    assert nxt is not None and (nxt.day, nxt.hour, nxt.minute) == (9, 0, 0)


@pytest.mark.parametrize("bad", ["@reboot", "@often"])
def test_unknown_nickname_fails_at_parse(bad):
    with pytest.raises(ValueError, match="isn't a time"):
        parse_when({"cron": bad})


def _fires(cron: str, tz: str, start: datetime, count: int) -> list[datetime]:
    trigger = when_to_trigger(When(cron=cron, timezone=tz))
    out: list[datetime] = []
    t = trigger.get_next_fire_time(None, start)
    while t is not None and len(out) < count:
        out.append(t)
        t = trigger.get_next_fire_time(t, later(t, timedelta(microseconds=1)))
    return out


def test_fixed_time_fires_once_when_the_clocks_go_back():
    # 25 Oct 2026: Europe/London repeats 01:00-02:00. cron runs a 01:30 job once.
    london = ZoneInfo("Europe/London")
    fires = _fires("30 1 * * *", "Europe/London", datetime(2026, 10, 24, 12, tzinfo=london), 3)
    days = [f.astimezone(london).date().isoformat() for f in fires]
    assert days == ["2026-10-25", "2026-10-26", "2026-10-27"]


def test_wildcard_hour_still_runs_through_the_repeated_hour():
    # A */30 job runs by elapsed time (as cron does): 01:00 and 01:30 BST, then
    # 01:00 and 01:30 GMT, half an hour apart throughout.
    start = datetime(2026, 10, 24, 23, 59, tzinfo=timezone.utc)  # 00:59 BST
    fires = _fires("*/30 * * * *", "Europe/London", start, 4)
    assert [f.astimezone(timezone.utc).strftime("%H:%M") for f in fires] == ["00:00", "00:30", "01:00", "01:30"]


def test_later_counts_real_time_in_the_repeated_hour():
    london = ZoneInfo("Europe/London")
    gmt_0130 = datetime(2026, 10, 25, 1, 30, tzinfo=london, fold=1)  # 01:30 GMT
    utc = timezone.utc
    # the trap: wall-clock arithmetic lands an hour earlier, at 01:30:01 BST
    assert (gmt_0130 + timedelta(seconds=1)).astimezone(utc) < gmt_0130.astimezone(utc)
    assert later(gmt_0130, timedelta(seconds=1)) == gmt_0130.astimezone(utc) + timedelta(seconds=1)


def test_fixed_time_in_the_skipped_hour_runs_after_the_jump():
    # 28 Mar 2027: Europe/London skips 01:00-02:00; a 01:30 job runs once that day.
    london = ZoneInfo("Europe/London")
    fires = _fires("30 1 * * *", "Europe/London", datetime(2027, 3, 27, 12, tzinfo=london), 2)
    assert [f.astimezone(london).date().isoformat() for f in fires] == ["2027-03-28", "2027-03-29"]


def test_same_zone():
    assert same_zone("Etc/UTC", "UTC")
    assert same_zone("Europe/London", "Europe/London")
    assert same_zone("Europe/Belfast", "Europe/London")  # an alias keeps the same clock
    assert not same_zone("Europe/London", "UTC")
    assert not same_zone("Europe/London", "")
    assert not same_zone("Not/AZone", "UTC")


def test_cron_zone_problem():
    assert cron_zone_problem("", host_zone="Europe/London") == ""
    assert cron_zone_problem("Europe/London", host_zone="Europe/London") == ""
    assert cron_zone_problem("UTC", host_zone="Etc/UTC") == ""
    problem = cron_zone_problem("Europe/London", host_zone="Etc/UTC")
    assert "needs the daemon backend" in problem and "Etc/UTC" in problem
    assert "can't be told" in cron_zone_problem("Europe/London", host_zone="")

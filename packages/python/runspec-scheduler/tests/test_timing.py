"""Timing tests: concrete oracle assertions + parity with runspec-console."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from runspec_scheduler.timing import (
    When,
    _parse_every,
    next_fire_time,
    parse_when,
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
    now = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
    nxt = next_fire_time(When(every="15m", timezone="UTC"), now=now)
    assert nxt is not None
    # An IntervalTrigger fires within one period of `now` (aligned to the
    # trigger's own start, not exactly now + interval).
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

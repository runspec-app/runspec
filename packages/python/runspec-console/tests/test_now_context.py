"""
test_now_context.py — the agent's current-date awareness.

The chat/trigger/schedule agent turns fold ``_now_context()`` into the system
prompt so the model resolves relative dates ("last Friday") against the
operator's real machine clock instead of guessing from its training cutoff.
"""

from __future__ import annotations

from datetime import datetime

from runspec_console.bridge import Bridge


class _Stub:
    _now_context = Bridge._now_context


def test_now_context_carries_local_weekday_and_iso_date():
    out = _Stub()._now_context()
    today = datetime.now()
    assert "## Current date" in out
    assert today.strftime("%Y-%m-%d") in out  # ISO date
    assert today.strftime("%A") in out  # weekday name, for "last Friday" reasoning
    assert "local time" in out


def test_now_context_is_date_only_no_clock_time():
    # Date-only keeps the system prompt byte-identical across a day's turns so the
    # model-side prompt cache keeps hitting — a clock time would churn it.
    out = _Stub()._now_context()
    now = datetime.now()
    assert now.strftime("%H:%M") not in out

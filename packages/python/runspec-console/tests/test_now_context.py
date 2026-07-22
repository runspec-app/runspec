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


class _BaseStub:
    _base_system = Bridge._base_system

    def __init__(self, cfg=None):
        self._cfg = cfg or {}

    def get_config(self):
        return self._cfg


def test_now_context_carries_local_weekday_and_iso_date():
    out = _Stub()._now_context()
    today = datetime.now()
    assert "## Current date" in out
    assert today.strftime("%Y-%m-%d") in out  # ISO date
    assert today.strftime("%A") in out  # weekday name, for "last Friday" reasoning
    assert "local time" in out


def test_now_context_carries_timezone_and_local_time_guidance():
    # The timezone anchor is what stops the agent defaulting to UTC for clock times.
    out = _Stub()._now_context()
    assert "UTC" in out  # the offset (e.g. "UTC-05:00" or "UTC offset unknown")
    assert "never UTC" in out  # explicit: produce clock times in local time
    assert "console_status" in out  # where to get the exact current time


def test_now_context_no_live_minute_clock():
    # Date + timezone (both stable across a day) are fine; a LIVE per-minute clock
    # time is not — it would churn the cached system prefix every turn. Scrub the
    # (stable) tz offset first so a coincidental match with the current minute
    # (e.g. it's 05:00 and the offset is UTC-05:00) doesn't false-positive.
    out = _Stub()._now_context()
    now = datetime.now().astimezone()
    raw = now.strftime("%z")
    offset_token = f"UTC{raw[:3]}:{raw[3:]}" if len(raw) == 5 else ""
    scrubbed = out.replace(offset_token, "")
    assert now.strftime("%H:%M") not in scrubbed


def test_base_system_default_prefers_clarifying_over_guessing():
    # Default (no [llm] system configured) tells the agent to ask rather than guess
    # when a required arg is ambiguous and getting it wrong would matter.
    text = _BaseStub()._base_system().lower()
    assert "clarifying question" in text and "instead of guessing" in text


def test_base_system_configured_prompt_wins():
    # A configured [llm] system replaces the default wholesale (guideline included).
    stub = _BaseStub({"llm": {"system": "Custom persona."}})
    assert stub._base_system() == "Custom persona."

"""
test_trigger_workers.py — the [automation] max_workers knob is threaded from
config into the TriggerEngine's thread pool by start_triggers().

The engine's own honouring of max_workers is covered in test_triggers.py; this
guards the one-line bridge plumb (config → TriggerEngine construction).
"""

from __future__ import annotations

import runspec_console.triggers as triggers_mod
from runspec_console.bridge import Bridge


class _CapturingEngine:
    """Records the max_workers it was built with, then no-ops its lifecycle."""

    last_max_workers: int | None = None

    def __init__(self, triggers, **kw):
        _CapturingEngine.last_max_workers = kw.get("max_workers")

    def start(self):
        return []

    def stop(self):
        return None

    def feed(self, *a, **k):
        return None


def _boot(monkeypatch, config: dict) -> int | None:
    b = Bridge()
    # start_triggers does `from .triggers import TriggerEngine` at call time, so
    # patch it on its home module.
    monkeypatch.setattr(triggers_mod, "TriggerEngine", _CapturingEngine)
    # Feed the config readers start_triggers pulls from.
    from runspec_console import config as config_mod

    monkeypatch.setattr(config_mod, "read_config", lambda: config)
    monkeypatch.setattr(
        config_mod,
        "read_triggers",
        lambda: [
            {
                "name": "t",
                "source": "chat",
                "event": "message",
                "mode": "rule",
                "run": "local__echo",
            }
        ],
    )
    monkeypatch.setattr(config_mod, "read_mailbox", lambda: None)
    monkeypatch.setattr(config_mod, "read_watches", lambda: [])
    monkeypatch.setattr(config_mod, "trigger_file_error", lambda: None)
    monkeypatch.setattr(b, "_read_schedules", lambda: [])
    monkeypatch.setattr(b, "_ensure_claim_poll", lambda: None)
    monkeypatch.setattr(b, "_base_system", lambda: "")
    monkeypatch.setattr(b, "_build_clock_ok", lambda: lambda: True)
    monkeypatch.setattr(b, "_build_lock_ok", lambda: lambda: True)
    _CapturingEngine.last_max_workers = None
    b.start_triggers()
    return _CapturingEngine.last_max_workers


def test_default_pool_is_two(monkeypatch):
    assert _boot(monkeypatch, {}) == 2


def test_configured_pool_size(monkeypatch):
    assert _boot(monkeypatch, {"automation": {"max_workers": 6}}) == 6


def test_pool_size_is_clamped(monkeypatch):
    assert _boot(monkeypatch, {"automation": {"max_workers": 999}}) == 32

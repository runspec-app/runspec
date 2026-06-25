"""Tests for the sounds package — the pure mapping logic + safe no-op behaviour.

OS-independent: the actual winsound playback only runs on Windows; everywhere
else ``available()`` is False and ``play()`` is a no-op. We exercise the pure
``resolve_target`` parser and the config fallbacks, plus the non-Windows guards.
"""

from __future__ import annotations

import sys

import pytest

from runspec_console import config, sounds


# ── resolve_target (pure) ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "token,expected",
    [
        ("synth:confirm", ("synth", "confirm")),
        ("synth:message", ("synth", "message")),
        ("  synth:reply  ", ("synth", "reply")),
        ("system:SystemAsterisk", ("system", "SystemAsterisk")),
        ("off", None),
        ("", None),
        ("   ", None),
        (None, None),
        ("synth:does-not-exist", None),  # unknown wav name
        ("system:", None),  # blank alias
        ("garbage", None),  # no scheme
        (42, None),  # wrong type
    ],
)
def test_resolve_target(token, expected):
    assert sounds.resolve_target(token) == expected


def test_categories_are_stable():
    assert sounds.categories() == (
        "confirm",
        "trigger",
        "completion_success",
        "completion_error",
        "completion_stopped",
        "reply",
        "message",
    )


def test_every_default_choice_resolves():
    # Every built-in default maps to a real bundled wav.
    for category in sounds.categories():
        token = config.DEFAULT_SOUND_MAP[category]
        target = sounds.resolve_target(token)
        assert target is not None and target[0] == "synth"


# ── config helpers ────────────────────────────────────────────────────────────


def test_sound_enabled_default(monkeypatch):
    monkeypatch.setattr(config, "read_config", lambda: {})
    assert config.sound_enabled() is True  # default on


def test_sound_enabled_override(monkeypatch):
    monkeypatch.setattr(
        config, "read_config", lambda: {"console": {"sounds": {"enabled": False}}}
    )
    assert config.sound_enabled() is False


def test_sound_choice_default_and_override(monkeypatch):
    monkeypatch.setattr(config, "read_config", lambda: {})
    assert config.sound_choice("confirm") == "synth:confirm"
    assert config.sound_choice("unknown-cat") == "off"  # no default -> off
    monkeypatch.setattr(
        config,
        "read_config",
        lambda: {"console": {"sounds": {"confirm": "system:SystemExclamation"}}},
    )
    assert config.sound_choice("confirm") == "system:SystemExclamation"
    assert config.sound_choice("trigger") == "synth:trigger"  # unset -> default


# ── safe no-op everywhere; real playback only on Windows ──────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows no-op path")
def test_available_false_off_windows():
    ok, reason = sounds.available()
    assert ok is False and reason


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows no-op path")
def test_play_is_noop_off_windows():
    assert sounds.play("confirm") is False
    assert sounds.play("confirm", force=True) is False
    assert sounds.play("not-a-category") is False  # never raises

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
        "host_up",
        "host_down",
        "room_up",
        "room_down",
    )


# ── file: tokens ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "token,expected",
    [
        ("file:C:/sounds/jarvis.wav", ("file", "C:/sounds/jarvis.wav")),
        ("file:/home/op/cue.mp3", ("file", "/home/op/cue.mp3")),
        (
            "file:C:/sounds/CLIP.MP3",
            ("file", "C:/sounds/CLIP.MP3"),
        ),  # case-insensitive ext
        ("file:", None),  # blank path
        ("file:C:/sounds/clip.ogg", None),  # unsupported extension
        ("file:C:/sounds/noext", None),
    ],
)
def test_resolve_target_file_tokens(token, expected):
    assert sounds.resolve_target(token) == expected


# ── sound packs ───────────────────────────────────────────────────────────────


def test_pack_lookup_prefers_wav_and_ignores_absent(tmp_path):
    (tmp_path / "confirm.wav").write_bytes(b"x")
    (tmp_path / "confirm.mp3").write_bytes(b"x")
    (tmp_path / "host_down.mp3").write_bytes(b"x")
    assert sounds.pack_lookup(tmp_path, "confirm") == tmp_path / "confirm.wav"
    assert sounds.pack_lookup(tmp_path, "host_down") == tmp_path / "host_down.mp3"
    assert sounds.pack_lookup(tmp_path, "trigger") is None
    assert sounds.pack_lookup("", "confirm") is None
    assert sounds.pack_lookup(tmp_path / "missing", "confirm") is None


def test_pack_coverage(tmp_path):
    (tmp_path / "confirm.wav").write_bytes(b"x")
    (tmp_path / "room_down.mp3").write_bytes(b"x")
    (tmp_path / "unrelated.txt").write_bytes(b"x")
    cov = sounds.pack_coverage(tmp_path)
    assert cov == {"confirm": "confirm.wav", "room_down": "room_down.mp3"}


def test_effective_resolution_precedence(tmp_path, monkeypatch):
    # explicit token > pack file > built-in default; explicit "off" silences
    # even with a pack present.
    (tmp_path / "confirm.wav").write_bytes(b"x")
    (tmp_path / "trigger.mp3").write_bytes(b"x")
    monkeypatch.setattr(
        config,
        "read_config",
        lambda: {
            "console": {
                "sounds": {
                    "pack": str(tmp_path),
                    "confirm": "system:SystemExclamation",  # explicit wins over pack
                    "reply": "off",  # explicit off wins over default
                }
            }
        },
    )
    assert sounds._resolve_effective("confirm") == ("system", "SystemExclamation")
    assert sounds._resolve_effective("trigger") == (
        "file",
        str(tmp_path / "trigger.mp3"),
    )
    assert sounds._resolve_effective("reply") is None
    assert sounds._resolve_effective("message") == ("synth", "message")  # default


# ── connection cue gate ───────────────────────────────────────────────────────


def test_gate_first_observation_is_silent():
    gate = sounds.ConnectionCueGate(cooldown=30)
    assert gate.note("host:web1", True, 100.0) is False
    assert gate.note("host:web1", True, 101.0) is False  # unchanged → silent


def test_gate_cues_on_flip_and_respects_cooldown():
    gate = sounds.ConnectionCueGate(cooldown=30)
    gate.note("host:web1", True, 100.0)  # first observation
    assert gate.note("host:web1", False, 110.0) is True  # flip → cue
    assert gate.note("host:web1", True, 115.0) is False  # flap inside cooldown
    assert gate.note("host:web1", False, 120.0) is False  # still inside cooldown
    assert gate.note("host:web1", True, 150.0) is True  # cooldown elapsed → cue


def test_gate_keys_are_independent():
    gate = sounds.ConnectionCueGate(cooldown=30)
    gate.note("host:a", True, 0.0)
    gate.note("host:b", True, 0.0)
    assert gate.note("host:a", False, 10.0) is True
    assert gate.note("host:b", False, 11.0) is True  # b's cooldown is its own


def test_gate_suppressed_flip_still_updates_state():
    gate = sounds.ConnectionCueGate(cooldown=30)
    gate.note("room:ops", True, 0.0)
    assert gate.note("room:ops", False, 5.0) is True  # cue
    assert gate.note("room:ops", True, 10.0) is False  # suppressed, state now up
    # No flip since state was recorded as up — down again after cooldown cues.
    assert gate.note("room:ops", False, 60.0) is True


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


def test_sound_volume(monkeypatch):
    monkeypatch.setattr(config, "read_config", lambda: {})
    assert config.sound_volume() == 100  # default
    for raw, want in [(55, 55), (0, 0), (250, 100), (-5, 0), (True, 100), ("x", 100)]:
        monkeypatch.setattr(
            config,
            "read_config",
            lambda raw=raw: {"console": {"sounds": {"volume": raw}}},
        )
        assert config.sound_volume() == want


def test_sound_choice_set_distinguishes_unset(monkeypatch):
    monkeypatch.setattr(config, "read_config", lambda: {})
    assert config.sound_choice_set("confirm") is None  # unset — pack may apply
    monkeypatch.setattr(
        config, "read_config", lambda: {"console": {"sounds": {"confirm": "off"}}}
    )
    assert config.sound_choice_set("confirm") == "off"  # explicit off is a choice


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

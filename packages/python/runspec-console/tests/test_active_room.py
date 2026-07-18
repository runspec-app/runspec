"""Tests for the machine-local single-room selector (active_room.py + the
bridge get_room_hosts / get_active_room / set_active_room methods).

The console may have several fleet hosts that each run the *same* helpdesk room;
joining more than one collides on the namespace, so this console picks exactly
one (a per-console, never-synced choice). These lock the selection rule and the
bridge wiring."""

from __future__ import annotations

import threading

import pytest

from runspec_console import config as config_mod
from runspec_console.active_room import (
    read_active_room,
    room_hosts,
    select_room_specs,
    write_active_room,
)
from runspec_console.bridge import Bridge


def _specs(*hosts: str) -> list[dict]:
    # One room spec per host (mirrors host_rooms' shape closely enough).
    return [{"host": h, "room": f"helpdesk/{h}", "queue": "helpdesk"} for h in hosts]


# ── pure filter ───────────────────────────────────────────────────────────────


def test_room_hosts_distinct_in_order():
    specs = _specs("web01", "web02") + _specs("web01")
    assert room_hosts(specs) == ["web01", "web02"]


def test_unset_single_host_auto_connects():
    # None = never set → default: the sole room host connects (back-compat).
    assert select_room_specs(_specs("web01"), None) == _specs("web01")


def test_unset_multiple_hosts_connects_none():
    # 2+ hosts and no explicit pick → connect nothing (no silent fan-out).
    assert select_room_specs(_specs("web01", "web02"), None) == []


def test_explicit_none_connects_nothing_even_with_one_host():
    # "" = explicitly detached, overrides the sole-host default.
    assert select_room_specs(_specs("web01"), "") == []


def test_explicit_host_selects_only_that_host():
    specs = _specs("web01", "web02")
    assert select_room_specs(specs, "web02") == _specs("web02")


def test_stale_selection_falls_back_to_default():
    # Selected host no longer configured → behave as unset, don't error.
    assert select_room_specs(_specs("web01"), "gone") == _specs("web01")
    assert select_room_specs(_specs("web01", "web02"), "gone") == []


# ── read/write pointer ────────────────────────────────────────────────────────


def test_read_write_roundtrip(tmp_path):
    p = tmp_path / "runspec_active_room.toml"
    assert read_active_room(p) is None  # absent → unset
    write_active_room(p, "web02")
    assert read_active_room(p) == "web02"
    write_active_room(p, "")  # explicit none
    assert read_active_room(p) == ""


# ── bridge methods ────────────────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    return tmp_path


class _StubBridge:
    def __init__(self, specs) -> None:
        self._specs = specs
        self._lock = threading.Lock()
        self.restarted = 0
        self.dispatched: list[tuple[str, dict]] = []

    def _room_specs(self):
        return self._specs

    def restart_chat(self):
        self.restarted += 1

    def _dispatch(self, event, detail):
        self.dispatched.append((event, detail))

    _active_room_specs = Bridge._active_room_specs
    get_room_hosts = Bridge.get_room_hosts
    get_active_room = Bridge.get_active_room
    set_active_room = Bridge.set_active_room


def test_get_room_hosts_lists_every_candidate(app):
    b = _StubBridge(_specs("web01", "web02"))
    assert [h["host"] for h in b.get_room_hosts()] == ["web01", "web02"]
    assert b.get_room_hosts()[0]["queue"] == "helpdesk"


def test_get_active_room_default_single(app):
    b = _StubBridge(_specs("web01"))
    assert b.get_active_room() == "web01"  # sole host auto-connects


def test_get_active_room_multiple_needs_pick(app):
    b = _StubBridge(_specs("web01", "web02"))
    assert b.get_active_room() == ""  # 2+ and unset → none until picked


def test_set_active_room_persists_and_restarts(app):
    b = _StubBridge(_specs("web01", "web02"))
    res = b.set_active_room("web02")
    assert res == {"active": "web02"}
    assert b.get_active_room() == "web02"
    assert b.restarted == 1
    assert b.dispatched and b.dispatched[-1][0] == "runspec:room_connection"


def test_set_active_room_none_detaches(app):
    b = _StubBridge(_specs("web01"))
    b.set_active_room("")  # explicit none
    assert b.get_active_room() == ""

"""Regression: saving a host through the Settings editor must not wipe other
hosts' profile tags.

``get_jump_hosts`` (the editor's load) omitted the per-host ``profiles`` array
while ``save_jump_hosts`` rebuilds every *managed* key on write — so a round-trip
through the editor dropped every host's profile assignments, and the operator had
to re-tag them all. These bind the real bridge methods onto a light stub and
point config paths at a tmp app dir so the TOML round-trips for real.
"""

from __future__ import annotations

import threading

import pytest

from runspec_console import config as config_mod
from runspec_console import hosts as hosts_mod
from runspec_console.bridge import Bridge


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    return tmp_path


class _StubBridge:
    def __init__(self) -> None:
        self._hosts: list[dict] = []
        self._lock = threading.Lock()
        self.dispatched: list[str] = []

    def restart_chat(self) -> None:  # save_jump_hosts re-joins rooms
        pass

    def _kick_refresh(self, *, force: bool = False) -> None:  # spawned by save
        pass

    def _dispatch(self, event: str, detail: dict) -> None:  # hosts_updated signal
        self.dispatched.append(event)

    _reload_hosts = Bridge._reload_hosts
    _MANAGED_HOST_KEYS = Bridge._MANAGED_HOST_KEYS
    get_jump_hosts = Bridge.get_jump_hosts
    save_jump_hosts = Bridge.save_jump_hosts


def _write(_app_dir, hosts) -> None:
    hosts_mod.save_hosts(config_mod.hosts_path(), hosts)


def _on_disk_profiles(name: str) -> list[str]:
    for h in hosts_mod.load_hosts(config_mod.hosts_path()):
        if h.get("name") == name:
            return hosts_mod.host_profiles(h)
    raise AssertionError(f"host {name!r} not found on disk")


def test_get_jump_hosts_includes_profiles(app):
    _write(app, [{"name": "h1", "ssh": "u@h1", "profiles": ["devops", "app-support"]}])
    (entry,) = _StubBridge().get_jump_hosts()
    assert entry["profiles"] == ["devops", "app-support"]


def test_get_jump_hosts_omits_empty_profiles(app):
    _write(app, [{"name": "h1", "ssh": "u@h1"}])
    (entry,) = _StubBridge().get_jump_hosts()
    assert "profiles" not in entry  # kept clean, like group/room


def test_saving_one_host_preserves_every_hosts_profiles(app):
    _write(
        app,
        [
            {"name": "h1", "ssh": "u@h1", "profiles": ["app-support"]},
            {"name": "h2", "ssh": "u@h2", "profiles": ["devops", "app-support"]},
        ],
    )
    b = _StubBridge()

    # The editor loads the full list, edits one host, and saves the list back.
    loaded = b.get_jump_hosts()
    for h in loaded:
        if h["name"] == "h1":
            h["user"] = "admin"  # any edit to h1
    b.save_jump_hosts(loaded)

    # Before the fix, both of these were [] after the save.
    assert _on_disk_profiles("h1") == ["app-support"]
    assert _on_disk_profiles("h2") == ["devops", "app-support"]


def test_save_signals_hosts_updated(app):
    # The host-list-changed signal lets consumers (the Self-Service room selector)
    # refetch, so a host whose room config was removed drops out without a restart.
    _write(app, [{"name": "h1", "ssh": "u@h1"}])
    b = _StubBridge()
    b.save_jump_hosts(b.get_jump_hosts())
    assert "runspec:hosts_updated" in b.dispatched

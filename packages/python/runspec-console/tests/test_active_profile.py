"""
test_active_profile.py — bridge-level host-profile behavior.

Covers the registry CRUD (self-heal / delete reconciliation / rename re-point),
the interactive-set scoping (`_active_host_entries`), and the profile switch
(`set_active_profile`: evict leaving hosts' transports, clear their dot, kick a
refresh). Binds the real bridge methods onto a light stub and points config paths
at a tmp app dir so the real TOML round-trips.
"""

from __future__ import annotations

import threading

import pytest

from runspec_console import config as config_mod
from runspec_console import hosts as hosts_mod
from runspec_console import profiles as profiles_mod
from runspec_console.bridge import Bridge


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Point every app_dir-derived config path at an isolated tmp dir."""
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    return tmp_path


class FakePool:
    def __init__(self) -> None:
        self.dropped: list[str] = []

    def drop(self, ssh_target, *, identity_file=None, global_ssh_config=None) -> None:
        self.dropped.append(ssh_target)


class _StubBridge:
    def __init__(self) -> None:
        self._hosts: list[dict] = []
        self._connected_cache: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._ssh_pool = FakePool()
        self.dispatched: list[str] = []
        self.refreshed = 0

    def get_config(self) -> dict:
        return {"ssh": {}}

    def _dispatch(self, event: str, detail: dict) -> None:
        self.dispatched.append(event)

    def _kick_refresh(self, *, force: bool = False) -> None:
        self.refreshed += 1
        self.refresh_forced = force

    _reload_hosts = Bridge._reload_hosts
    _local_host_entry = Bridge._local_host_entry
    _host_entry = Bridge._host_entry
    _all_host_entries = Bridge._all_host_entries
    _active_profile_name = Bridge._active_profile_name
    _active_host_entries = Bridge._active_host_entries
    get_profiles = Bridge.get_profiles
    save_profiles = Bridge.save_profiles
    rename_profile = Bridge.rename_profile
    get_active_profile = Bridge.get_active_profile
    set_active_profile = Bridge.set_active_profile
    set_host_profile = Bridge.set_host_profile
    get_other_hosts = Bridge.get_other_hosts
    _drop_hosts_leaving_scope = Bridge._drop_hosts_leaving_scope


def _write_hosts(app_dir, hosts):
    hosts_mod.save_hosts(config_mod.hosts_path(), hosts)


def _names(entries):
    return [e["name"] for e in entries]


def test_get_profiles_self_heals_from_host_tags(app):
    _write_hosts(
        app,
        [
            {"name": "h1", "ssh": "u@h1", "profiles": ["app-support"]},
            {"name": "h2", "ssh": "u@h2", "profiles": ["devops", "app-support"]},
        ],
    )
    b = _StubBridge()

    registry = b.get_profiles()

    names = {p["name"] for p in registry}
    assert names == {"app-support", "devops"}
    # Persisted so it syncs — a second call reads them from the file too.
    on_disk = {
        p["name"] for p in profiles_mod.load_profiles(config_mod.profiles_path())
    }
    assert on_disk == {"app-support", "devops"}


def test_active_host_entries_scopes_to_active_profile(app):
    _write_hosts(
        app,
        [
            {"name": "h1", "ssh": "u@h1", "profiles": ["app-support"]},
            {"name": "h2", "ssh": "u@h2", "profiles": ["devops"]},
            {"name": "h3", "ssh": "u@h3", "profiles": ["app-support", "devops"]},
            {"name": "h4", "ssh": "u@h4"},  # untagged
        ],
    )
    b = _StubBridge()

    # All hosts (no active profile) → everything in scope (+ local).
    assert _names(b._active_host_entries()) == ["local", "h1", "h2", "h3", "h4"]

    b.set_active_profile("app-support")
    assert b.get_active_profile() == "app-support"
    assert _names(b._active_host_entries()) == ["local", "h1", "h3"]
    # The full set is untouched — automation still reaches every host.
    assert _names(b._all_host_entries()) == ["local", "h1", "h2", "h3", "h4"]


def test_switch_evicts_leaving_hosts_and_kicks_refresh(app):
    _write_hosts(
        app,
        [
            {"name": "h1", "ssh": "u@h1", "profiles": ["app-support"]},
            {"name": "h2", "ssh": "u@h2", "profiles": ["devops"]},
            {"name": "h3", "ssh": "u@h3", "profiles": ["app-support", "devops"]},
        ],
    )
    b = _StubBridge()
    b._connected_cache = {"h1": True, "h2": True, "h3": True}

    res = b.set_active_profile("app-support")

    assert res == {"active": "app-support"}
    # h2 left scope → its transport is evicted and its dot cleared.
    assert b._ssh_pool.dropped == ["u@h2"]
    assert "h2" not in b._connected_cache
    assert b._connected_cache.get("h1") is True  # still in scope, untouched
    assert "runspec:hosts_updated" in b.dispatched
    assert b.refreshed == 1
    assert b.refresh_forced is True  # a switch re-discovers the new scope


def test_save_profiles_delete_strips_tag_and_resets_active(app):
    _write_hosts(
        app,
        [
            {"name": "h1", "ssh": "u@h1", "profiles": ["app-support"]},
            {"name": "h3", "ssh": "u@h3", "profiles": ["app-support", "devops"]},
        ],
    )
    b = _StubBridge()
    b.set_active_profile("devops")  # active is the profile we're about to delete

    # Keep only app-support; devops is deleted.
    b.save_profiles([{"name": "app-support"}])

    hosts = hosts_mod.load_hosts(config_mod.hosts_path())
    by_name = {h["name"]: h for h in hosts}
    assert hosts_mod.host_profiles(by_name["h1"]) == ["app-support"]
    assert hosts_mod.host_profiles(by_name["h3"]) == ["app-support"]  # devops stripped
    # Active fell back to All hosts since its profile is gone.
    assert b.get_active_profile() == ""


def test_rename_profile_repoints_membership_and_active(app):
    _write_hosts(
        app,
        [
            {"name": "h1", "ssh": "u@h1", "profiles": ["app-support"]},
            {"name": "h3", "ssh": "u@h3", "profiles": ["app-support", "devops"]},
        ],
    )
    profiles_mod.save_profiles(
        config_mod.profiles_path(), [{"name": "app-support"}, {"name": "devops"}]
    )
    b = _StubBridge()
    b.set_active_profile("app-support")

    b.rename_profile("app-support", "AppSupport")

    hosts = hosts_mod.load_hosts(config_mod.hosts_path())
    by_name = {h["name"]: h for h in hosts}
    assert hosts_mod.host_profiles(by_name["h1"]) == ["AppSupport"]
    assert hosts_mod.host_profiles(by_name["h3"]) == ["AppSupport", "devops"]
    names = {p["name"] for p in profiles_mod.load_profiles(config_mod.profiles_path())}
    assert names == {"AppSupport", "devops"}
    assert b.get_active_profile() == "AppSupport"  # pointer followed the rename


def test_other_hosts_lists_hosts_outside_the_active_profile(app):
    _write_hosts(
        app,
        [
            {"name": "h1", "ssh": "u@h1", "group": "prod", "profiles": ["app-support"]},
            {"name": "h2", "ssh": "u@h2", "group": "devops", "profiles": ["devops"]},
            {"name": "h3", "ssh": "u@h3"},  # untagged
        ],
    )
    b = _StubBridge()

    # All hosts: nothing is outside the profile.
    assert b.get_other_hosts() == []

    b.set_active_profile("app-support")
    other = b.get_other_hosts()
    assert _names(other) == ["h2", "h3"]
    assert other[0] == {
        "name": "h2",
        "group": "devops",
        "profiles": ["devops"],
        "role": None,
    }
    assert other[1]["profiles"] == []
    # Listing them connects to nothing.
    assert "h2" not in b._connected_cache


def test_set_host_profile_adds_host_to_active_profile(app):
    _write_hosts(
        app,
        [
            {"name": "h1", "ssh": "u@h1", "profiles": ["app-support"]},
            {"name": "h2", "ssh": "u@h2", "profiles": ["devops"], "role": "secondary"},
        ],
    )
    b = _StubBridge()
    b.set_active_profile("app-support")
    b.refreshed = 0
    b.dispatched.clear()

    res = b.set_host_profile("h2", "app-support", True)

    assert res == {"host": "h2", "profiles": ["devops", "app-support"]}
    on_disk = {h["name"]: h for h in hosts_mod.load_hosts(config_mod.hosts_path())}
    assert on_disk["h2"]["profiles"] == ["devops", "app-support"]
    assert on_disk["h2"]["role"] == "secondary"  # other keys untouched
    assert _names(b._active_host_entries()) == ["local", "h1", "h2"]
    assert b.get_other_hosts() == []
    assert "runspec:hosts_updated" in b.dispatched
    assert b.refreshed == 1


def test_set_host_profile_remove_drops_connection(app):
    _write_hosts(
        app,
        [
            {"name": "h1", "ssh": "u@h1", "profiles": ["app-support"]},
            {"name": "h2", "ssh": "u@h2", "profiles": ["app-support"]},
        ],
    )
    b = _StubBridge()
    b.set_active_profile("app-support")
    b._connected_cache["h1"] = True

    res = b.set_host_profile("h1", "app-support", False)

    assert res == {"host": "h1", "profiles": []}
    on_disk = {h["name"]: h for h in hosts_mod.load_hosts(config_mod.hosts_path())}
    assert "profiles" not in on_disk["h1"]
    assert b._ssh_pool.dropped == ["u@h1"]
    assert "h1" not in b._connected_cache
    assert _names(b.get_other_hosts()) == ["h1"]


def test_set_host_profile_no_change_and_errors(app):
    _write_hosts(app, [{"name": "h1", "ssh": "u@h1", "profiles": ["App-Support"]}])
    b = _StubBridge()

    # Already a member (case-insensitive): nothing written, no refresh.
    assert b.set_host_profile("h1", "app-support", True) == {
        "host": "h1",
        "profiles": ["App-Support"],
    }
    assert b.refreshed == 0
    with pytest.raises(ValueError):
        b.set_host_profile("nope", "app-support", True)
    with pytest.raises(ValueError):
        b.set_host_profile("local", "app-support", True)
    with pytest.raises(ValueError):
        b.set_host_profile("h1", " ", True)

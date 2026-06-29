"""
test_profiles.py — the host-profile registry module + hosts resolvers.

Profiles are a named host working-set (see docs/design/host-profiles.md). This
covers the pure pieces: the registry read/write/normalise (mirrors groups), the
machine-local active-profile pointer, and the membership resolvers on host
entries (`profiles` array tags).
"""

from __future__ import annotations

from runspec_console import hosts as hosts_mod
from runspec_console import profiles as profiles_mod


# ── registry (runspec_profiles.toml) ─────────────────────────────────────────


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "runspec_profiles.toml"
    profiles_mod.save_profiles(
        path,
        [
            {"name": "app-support", "description": "App fleet"},
            {"name": "devops"},
        ],
    )
    loaded = profiles_mod.load_profiles(path)
    assert [p["name"] for p in loaded] == ["app-support", "devops"]
    assert loaded[0]["description"] == "App fleet"


def test_load_absent_file_is_empty(tmp_path):
    assert profiles_mod.load_profiles(tmp_path / "nope.toml") == []


def test_normalise_drops_blank_and_dedups_case_insensitively():
    out = profiles_mod.normalise_profiles(
        [
            {"name": "App"},
            {"name": "  "},
            {"name": "app"},  # dup of App (case-insensitive) — dropped
            {"name": "DevOps", "description": "x"},
        ]
    )
    assert [p["name"] for p in out] == ["App", "DevOps"]


def test_profile_names_in_registry_order():
    names = profiles_mod.profile_names([{"name": "b"}, {"name": "a"}])
    assert names == ["b", "a"]


# ── active-profile pointer (machine-local) ───────────────────────────────────


def test_active_profile_roundtrip(tmp_path):
    path = tmp_path / "runspec_active_profile.toml"
    assert profiles_mod.read_active_profile(path) == ""  # absent => All hosts
    profiles_mod.write_active_profile(path, "devops")
    assert profiles_mod.read_active_profile(path) == "devops"
    profiles_mod.write_active_profile(path, "")  # clear => All hosts
    assert profiles_mod.read_active_profile(path) == ""


def test_active_profile_handles_garbage(tmp_path):
    path = tmp_path / "runspec_active_profile.toml"
    path.write_text("this is not = valid = toml", encoding="utf-8")
    assert profiles_mod.read_active_profile(path) == ""


# ── resolvers on host entries ────────────────────────────────────────────────


def test_host_profiles_reads_array():
    assert hosts_mod.host_profiles({"profiles": ["a", " b ", "", 3]}) == ["a", "b", "3"]
    assert hosts_mod.host_profiles({}) == []
    assert hosts_mod.host_profiles({"profiles": "notalist"}) == []


def test_host_in_profile_case_insensitive():
    h = {"profiles": ["App-Support"]}
    assert hosts_mod.host_in_profile(h, "app-support") is True
    assert hosts_mod.host_in_profile(h, "devops") is False
    # Blank profile == All hosts: every host is in scope.
    assert hosts_mod.host_in_profile({}, "") is True


def test_profile_members_and_blank_returns_all():
    hosts = [
        {"name": "h1", "profiles": ["a"]},
        {"name": "h2", "profiles": ["b"]},
        {"name": "h3", "profiles": ["a", "b"]},
        {"name": "h4"},  # untagged — only under All hosts
    ]
    assert hosts_mod.profile_members(hosts, "a") == ["h1", "h3"]
    assert hosts_mod.profile_members(hosts, "b") == ["h2", "h3"]
    # Blank profile (All hosts) returns every named host, untagged included.
    assert hosts_mod.profile_members(hosts, "") == ["h1", "h2", "h3", "h4"]


def test_profiles_map():
    hosts = [
        {"name": "h1", "profiles": ["a"]},
        {"name": "h2", "profiles": ["a", "b"]},
        {"name": "h3"},
    ]
    assert hosts_mod.profiles_map(hosts) == {"a": ["h1", "h2"], "b": ["h2"]}

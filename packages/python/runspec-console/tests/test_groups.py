"""
test_groups.py — the host-group registry (runspec_groups.toml).

Covers the pure load/save/normalise helpers and the bridge CRUD seam (get_groups
absorbing in-use host groups, save_groups reconciling deleted-group membership,
rename_group re-pointing member hosts) bound onto a bare stub, plus the Config
Sync wiring.
"""

from __future__ import annotations

import threading
import types

import pytest

from runspec_console import configmerge, gitsync
from runspec_console.bridge import Bridge
from runspec_console.groups import (
    group_names,
    load_groups,
    normalise_groups,
    save_groups,
)
from runspec_console.hosts import load_hosts, save_hosts

# ── pure helpers ──────────────────────────────────────────────────────────────


def test_groups_roundtrip_with_description(tmp_path):
    p = tmp_path / "runspec_groups.toml"
    save_groups(
        p, [{"name": "Production", "description": "Prod fleet"}, {"name": "Stage"}]
    )
    rows = load_groups(p)
    # Empty descriptions are omitted on write, so the raw load reflects that;
    # normalise_groups is what backfills "" for downstream consumers.
    assert rows == [
        {"name": "Production", "description": "Prod fleet"},
        {"name": "Stage"},
    ]
    assert group_names(rows) == ["Production", "Stage"]


def test_load_groups_absent_file(tmp_path):
    assert load_groups(tmp_path / "nope.toml") == []


def test_normalise_dedupes_case_insensitively_and_drops_blanks():
    rows = normalise_groups(
        [{"name": "Prod"}, {"name": "  "}, {"name": "prod"}, {"name": "Stage"}]
    )
    assert group_names(rows) == ["Prod", "Stage"]


# ── Config Sync wiring ────────────────────────────────────────────────────────


def test_groups_file_is_synced_and_merges_by_name():
    assert "runspec_groups.toml" in gitsync.KNOWN_FILES
    assert configmerge.COLLECTION_FILES["runspec_groups.toml"] == ("group", "name")


# ── bridge CRUD (bare stub) ───────────────────────────────────────────────────


def _stub(tmp_path, monkeypatch, *, hosts):
    monkeypatch.setattr(
        "runspec_console.config.hosts_path", lambda: tmp_path / "runspec_hosts.toml"
    )
    monkeypatch.setattr(
        "runspec_console.config.groups_path", lambda: tmp_path / "runspec_groups.toml"
    )
    save_hosts(tmp_path / "runspec_hosts.toml", hosts)
    ns = types.SimpleNamespace()
    for name in ("get_groups", "save_groups", "rename_group", "_reload_hosts"):
        setattr(ns, name, types.MethodType(getattr(Bridge, name), ns))
    ns._all_host_entries = lambda: load_hosts(tmp_path / "runspec_hosts.toml")
    ns._lock = threading.Lock()
    ns._dispatch = lambda *a, **k: None
    ns._reload_hosts()
    return ns


def test_get_groups_absorbs_in_use_host_groups(tmp_path, monkeypatch):
    ns = _stub(
        tmp_path,
        monkeypatch,
        hosts=[
            {"name": "web01", "group": "Production", "ssh": "x"},
            {"name": "db01", "group": "Legacy", "ssh": "y"},  # not yet registered
        ],
    )
    # Registry seeded with only Production.
    save_groups(tmp_path / "runspec_groups.toml", [{"name": "Production"}])
    out = ns.get_groups()
    names = [g["name"] for g in out]
    assert names == ["Production", "Legacy"]  # Legacy absorbed
    # …and persisted so it syncs.
    assert group_names(load_groups(tmp_path / "runspec_groups.toml")) == [
        "Production",
        "Legacy",
    ]


def test_save_groups_delete_resets_orphaned_hosts_to_ungrouped(tmp_path, monkeypatch):
    ns = _stub(
        tmp_path,
        monkeypatch,
        hosts=[
            {"name": "web01", "group": "Production", "ssh": "x"},
            {"name": "db01", "group": "Stage", "ssh": "y"},
        ],
    )
    save_groups(
        tmp_path / "runspec_groups.toml",
        [{"name": "Production"}, {"name": "Stage"}],
    )
    # Delete "Stage" by saving a registry without it.
    ns.save_groups([{"name": "Production"}])
    hosts = {h["name"]: h for h in load_hosts(tmp_path / "runspec_hosts.toml")}
    assert hosts["web01"].get("group") == "Production"  # untouched
    assert "group" not in hosts["db01"]  # orphaned → ungrouped


def test_rename_group_repoints_member_hosts(tmp_path, monkeypatch):
    ns = _stub(
        tmp_path,
        monkeypatch,
        hosts=[
            {"name": "web01", "group": "Prod", "ssh": "x"},
            {"name": "web02", "group": "Prod", "ssh": "y"},
            {"name": "db01", "group": "Stage", "ssh": "z"},
        ],
    )
    save_groups(tmp_path / "runspec_groups.toml", [{"name": "Prod"}, {"name": "Stage"}])
    ns.rename_group("Prod", "Production")
    assert group_names(load_groups(tmp_path / "runspec_groups.toml")) == [
        "Production",
        "Stage",
    ]
    hosts = {h["name"]: h for h in load_hosts(tmp_path / "runspec_hosts.toml")}
    assert hosts["web01"]["group"] == "Production"
    assert hosts["web02"]["group"] == "Production"
    assert hosts["db01"]["group"] == "Stage"  # untouched


def test_rename_group_requires_both_names(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch, hosts=[])
    with pytest.raises(ValueError):
        ns.rename_group("Prod", "")

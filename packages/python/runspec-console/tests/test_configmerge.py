"""
test_configmerge.py — the pure non-destructive merge helpers behind Config Sync.

Mappings deep-overlay (repo wins, local-only keys survive, preserve paths keep
local); collections merge by identity (add new, update matching, keep local-only,
preserve per-entry fields). No I/O — dict/list in, dict/list out.
"""

from __future__ import annotations

from runspec_console import configmerge as cm


def test_parse_preserve_groups_by_file_and_dedupes():
    parsed = cm.parse_preserve(
        [
            "config.toml:mailbox.folder",
            "runspec_hosts.toml:*.room_user",
            "config.toml:mailbox.folder",  # dup
            "  bogus-no-colon  ",
            "config.toml:",  # empty path
        ]
    )
    assert parsed["config.toml"] == [["mailbox", "folder"]]
    assert parsed["runspec_hosts.toml"] == [["*", "room_user"]]


def test_deep_overlay_repo_wins_local_only_survives():
    local = {
        "llm": {"provider": "anthropic", "api_key": "sk-local"},
        "ssh": {"user": "me"},
    }
    incoming = {"llm": {"provider": "openai"}, "console": {"theme": "dark"}}
    out = cm.deep_overlay(local, incoming)
    assert out["llm"]["provider"] == "openai"  # repo wins on conflict
    assert out["llm"]["api_key"] == "sk-local"  # local-only nested key survives
    assert out["ssh"]["user"] == "me"  # local-only section survives
    assert out["console"]["theme"] == "dark"  # repo-only section added


def test_merge_mapping_preserve_keeps_local_value():
    local = {"mailbox": {"address": "me@corp.com", "folder": "Tickets/Mine"}}
    incoming = {"mailbox": {"address": "team@corp.com", "folder": "Inbox"}}
    out = cm.merge_mapping(local, incoming, [["mailbox", "folder"]])
    assert out["mailbox"]["address"] == "team@corp.com"  # not preserved → repo wins
    assert out["mailbox"]["folder"] == "Tickets/Mine"  # preserved → local wins


def test_merge_mapping_preserve_drops_repo_value_when_local_absent():
    # A preserved key the repo tries to introduce must not land locally.
    local = {"mailbox": {"address": "me@corp.com"}}
    incoming = {"mailbox": {"address": "me@corp.com", "folder": "Inbox"}}
    out = cm.merge_mapping(local, incoming, [["mailbox", "folder"]])
    assert "folder" not in out["mailbox"]


def test_merge_collection_add_update_keep_local_only():
    local = [
        {"name": "mine", "pipeline": "local"},
        {"name": "shared", "pipeline": "old"},
    ]
    incoming = [
        {"name": "shared", "pipeline": "new"},
        {"name": "from-repo", "pipeline": "repo"},
    ]
    out = {
        e["name"]: e["pipeline"]
        for e in cm.merge_collection(local, incoming, "name", [])
    }
    assert out["mine"] == "local"  # local-only kept
    assert out["shared"] == "new"  # matching updated (repo wins)
    assert out["from-repo"] == "repo"  # new added


def test_merge_collection_preserves_per_entry_field_with_wildcard():
    local = [{"name": "web1", "ssh": "old@web1", "room_user": "alice"}]
    incoming = [{"name": "web1", "ssh": "new@web1"}]
    out = cm.merge_collection(local, incoming, "name", [["*", "room_user"]])
    assert out[0]["ssh"] == "new@web1"  # repo wins on normal field
    assert out[0]["room_user"] == "alice"  # preserved field kept from local


def test_merge_collection_preserve_targets_named_entry():
    local = [
        {"id": "a", "secret_arg": "keepme"},
        {"id": "b", "secret_arg": "x"},
    ]
    incoming = [
        {"id": "a", "secret_arg": "repo"},
        {"id": "b", "secret_arg": "repo"},
    ]
    out = {
        e["id"]: e["secret_arg"]
        for e in cm.merge_collection(local, incoming, "id", [["a", "secret_arg"]])
    }
    assert out["a"] == "keepme"  # only entry "a" preserved
    assert out["b"] == "repo"  # entry "b" takes repo value


# ── fold_layers (shared base + overlay) ──────────────────────────────────────────


def test_fold_layers_mapping_deep_overlays_in_order():
    base = {"llm": {"provider": "anthropic", "base_only": "x"}}
    team = {"llm": {"provider": "openai"}}
    eff = cm.fold_layers("config.toml", [base, team])
    assert eff["llm"]["provider"] == "openai"  # later layer wins
    assert eff["llm"]["base_only"] == "x"  # earlier-only key survives


def test_fold_layers_collection_merges_by_identity():
    base = {"host": [{"name": "web1", "ssh": "base"}, {"name": "db", "ssh": "base"}]}
    team = {"host": [{"name": "web1", "ssh": "team"}, {"name": "web2", "ssh": "team"}]}
    eff = cm.fold_layers("runspec_hosts.toml", [base, team])
    hosts = {h["name"]: h["ssh"] for h in eff["host"]}
    assert hosts == {"web1": "team", "db": "base", "web2": "team"}


def test_fold_layers_single_layer_is_identity():
    one = {"runbook": [{"id": "a"}]}
    assert cm.fold_layers("runspec_runbooks.toml", [one]) == one

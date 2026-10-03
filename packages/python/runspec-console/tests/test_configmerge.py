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


# ── seed_overrides (what a re-assert would silently revert) ───────────────────────


def test_seed_overrides_mapping_reports_only_differing_existing_keys():
    seed = {
        "llm": {"provider": "mycorp", "api_key_command": "get-key"},
        "new": {"k": 1},
    }
    local = {"llm": {"provider": "mycorp", "api_key_command": "C:/venv/Scripts/gk"}}
    rows = cm.seed_overrides("config.toml", seed, local, [])
    # provider matches (no row); api_key_command differs (a row); llm.* new key and
    # the whole [new] table are ADDs (local unset) → omitted.
    assert [r["path"] for r in rows] == ["llm.api_key_command"]
    r = rows[0]
    assert r["file"] == "config.toml"
    assert r["seed"] == "get-key"
    assert r["local"] == "C:/venv/Scripts/gk"
    assert r["preserved"] is False


def test_seed_overrides_mapping_flags_preserved_leaf():
    seed = {"llm": {"api_key_command": "get-key"}}
    local = {"llm": {"api_key_command": "my-local-cmd"}}
    preserve = [["llm", "api_key_command"]]
    rows = cm.seed_overrides("config.toml", seed, local, preserve)
    assert len(rows) == 1 and rows[0]["preserved"] is True


def test_seed_overrides_mapping_preserving_a_table_covers_its_leaves():
    seed = {"llm": {"api_key_command": "get-key"}}
    local = {"llm": {"api_key_command": "mine"}}
    rows = cm.seed_overrides("config.toml", seed, local, [["llm"]])
    assert rows[0]["preserved"] is True  # preserving [llm] protects the leaf


def test_seed_overrides_collection_diffs_by_identity_field():
    seed = {"credential": [{"id": "svc", "label": "Svc", "username": "seed-user"}]}
    local = {"credential": [{"id": "svc", "label": "Svc", "username": "real-user"}]}
    rows = cm.seed_overrides("runspec_credentials.toml", seed, local, [])
    assert [r["path"] for r in rows] == ["svc.username"]
    assert rows[0]["seed"] == "seed-user" and rows[0]["local"] == "real-user"


def test_seed_overrides_collection_new_entry_or_field_is_not_an_override():
    seed = {
        "credential": [
            {"id": "svc", "label": "Svc", "username": "u", "secret_arg": "pw"},
            {"id": "brand-new", "label": "New"},
        ]
    }
    local = {"credential": [{"id": "svc", "label": "Svc", "username": "u"}]}
    rows = cm.seed_overrides("runspec_credentials.toml", seed, local, [])
    # svc.username matches; svc.secret_arg is a new field (add); brand-new is a new
    # entry (add) → nothing reported.
    assert rows == []


def test_seed_overrides_collection_flags_preserved_field_wildcard():
    seed = {"credential": [{"id": "svc", "username": "seed-user"}]}
    local = {"credential": [{"id": "svc", "username": "real-user"}]}
    rows = cm.seed_overrides(
        "runspec_credentials.toml", seed, local, [["*", "username"]]
    )
    assert rows[0]["preserved"] is True


def test_seed_overrides_absent_local_yields_nothing():
    seed = {"llm": {"api_key_command": "get-key"}}
    assert cm.seed_overrides("config.toml", seed, {}, []) == []


def test_seed_overrides_truncates_long_values():
    seed = {"a": {"b": "x" * 500}}
    local = {"a": {"b": "y"}}
    rows = cm.seed_overrides("config.toml", seed, local, [])
    assert len(rows[0]["seed"]) == 200 and rows[0]["seed"].endswith("…")


# ── merged_native / change_summary / render_diff (pull preview) ─────────────────


def test_merged_native_collection_keeps_local_and_other_keys():
    from runspec_console.configmerge import merged_native

    local = {"host": [{"name": "a", "x": 1}, {"name": "mine"}], "version": 1}
    incoming = {"host": [{"name": "a", "x": 2}, {"name": "b"}]}
    out = merged_native("runspec_hosts.toml", local, incoming, [])
    assert [h["name"] for h in out["host"]] == ["a", "mine", "b"]
    assert out["host"][0]["x"] == 2
    assert out["version"] == 1


def test_merged_native_triggers_by_name_without_rota():
    from runspec_console.configmerge import merged_native

    local = {"triggers": {"mine": {"run": "x"}, "shared": {"run": "old"}}}
    incoming = {"triggers": {"shared": {"run": "new"}}, "rota": {"days": ["mon"]}}
    out = merged_native("runspec_triggers.toml", local, incoming, [])
    assert out == {"triggers": {"mine": {"run": "x"}, "shared": {"run": "new"}}}


def test_change_summary_collection_and_mapping():
    from runspec_console.configmerge import change_summary

    s = change_summary(
        "runspec_hosts.toml",
        {"host": [{"name": "a", "x": 1}, {"name": "k"}]},
        {"host": [{"name": "a", "x": 2}, {"name": "k"}, {"name": "b"}]},
    )
    assert s["changed"] and s["added"] == ["b"] and s["unchanged"] == 1
    assert s["updated"] == [{"id": "a", "fields": ["x"]}]

    m = change_summary(
        "config.toml",
        {"llm": {"provider": "a"}, "gone": 1},
        {"llm": {"provider": "b", "model": "m"}, "gone": 1},
    )
    assert m["updated"] == ["llm.provider"] and m["added"] == ["llm.model"]
    assert m["removed"] == []
    same = change_summary("config.toml", {"a": 1}, {"a": 1})
    assert same["changed"] is False


def test_render_diff_is_line_per_field_and_valid_toml():
    import tomllib

    from runspec_console.configmerge import _render_toml, render_diff

    before = {"host": [{"name": "a", "hostname": "old"}]}
    after = {"host": [{"name": "a", "hostname": "new"}]}
    d = render_diff("runspec_hosts.toml", before, after, to_label="v2")
    assert '-hostname = "old"' in d and '+hostname = "new"' in d
    assert "+++ v2/runspec_hosts.toml" in d
    assert render_diff("runspec_hosts.toml", after, after) == ""
    nested = {
        "llm": {"provider": "x", "extra": {"k": [1, 2]}},
        "mcp": {"servers": [{"name": "s", "env": {"A": "1"}}]},
        "triggers": {"my trig": {"run": "r"}},
    }
    assert tomllib.loads(_render_toml("config.toml", nested)) == nested

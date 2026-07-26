"""
test_savedruns.py — the saved-run registry (runspec_saved_runs.toml).

Covers parse_saved_run validation (exactly-one targeting, params table checks,
param/fixed-arg collision), the merged_args value validation + merge, the
load/save round-trip, and the Config Sync wiring.
"""

from __future__ import annotations

import pytest

from runspec_console import configmerge, gitsync, savedruns
from runspec_console.savedruns import (
    SavedRun,
    load_saved_run,
    load_saved_runs,
    merged_args,
    parse_saved_run,
    save_saved_runs,
)

# ── parse_saved_run ───────────────────────────────────────────────────────────


def test_parse_full_entry():
    entry = parse_saved_run(
        {
            "id": "restart-web",
            "title": "Restart web tier",
            "description": "Rolling restart",
            "runnable": "service-restart",
            "command": ["svc", "restart"],
            "group": "Production",
            "args": {"service": "nginx"},
            "params": {"reason": {"required": True}},
            "credential_ids": ["c1"],
        }
    )
    assert entry.id == "restart-web"
    assert entry.runnable == "service-restart"
    assert entry.command == ["svc", "restart"]
    assert entry.group == "Production"
    assert entry.args == {"service": "nginx"}
    assert entry.credential_ids == ["c1"]
    assert entry.target_summary() == "group Production"


def test_parse_command_accepts_a_bare_string():
    entry = parse_saved_run(
        {"id": "x", "runnable": "r", "host": "h", "command": "status"}
    )
    assert entry.command == ["status"]


def test_parse_requires_id_and_runnable():
    with pytest.raises(ValueError, match="missing an 'id'"):
        parse_saved_run({"runnable": "r", "host": "h"})
    with pytest.raises(ValueError, match="missing 'runnable'"):
        parse_saved_run({"id": "x", "host": "h"})


@pytest.mark.parametrize(
    "targeting",
    [
        {},  # none
        {"host": "h", "group": "g"},  # two
        {"hosts": ["a"], "selected_hosts": ["b"]},  # two (lists)
    ],
)
def test_parse_requires_exactly_one_targeting_mode(targeting):
    with pytest.raises(ValueError, match="exactly one of"):
        parse_saved_run({"id": "x", "runnable": "r", **targeting})


def test_parse_each_targeting_mode_and_summary():
    assert (
        parse_saved_run({"id": "x", "runnable": "r", "host": "web01"}).target_summary()
        == "web01"
    )
    assert (
        parse_saved_run(
            {"id": "x", "runnable": "r", "hosts": ["a", "b"]}
        ).target_summary()
        == "first connected of a, b"
    )
    assert (
        parse_saved_run(
            {"id": "x", "runnable": "r", "selected_hosts": ["a", "b"]}
        ).target_summary()
        == "all of a, b"
    )


def test_parse_rejects_param_shadowing_a_fixed_arg():
    # Dash/underscore spellings collide too (arg names normalise to underscores).
    with pytest.raises(ValueError, match="shadow"):
        parse_saved_run(
            {
                "id": "x",
                "runnable": "r",
                "host": "h",
                "args": {"log-level": "info"},
                "params": {"log_level": {"required": True}},
            }
        )


def test_parse_rejects_a_malformed_params_table():
    # A reserved arg-name prefix is invalid in the core grammar. (A bare scalar
    # like mode = "fast" is fine — legal shorthand for a default.)
    with pytest.raises(ValueError, match="bad params table"):
        parse_saved_run(
            {
                "id": "x",
                "runnable": "r",
                "host": "h",
                "params": {"runspec_mode": {"required": True}},
            }
        )


# ── merged_args ───────────────────────────────────────────────────────────────


def _entry(**kw) -> SavedRun:
    base = {"id": "x", "runnable": "r", "host": "h"}
    base.update(kw)
    return parse_saved_run(base)


def test_merged_args_fills_open_params_over_fixed_args():
    entry = _entry(
        args={"service": "nginx"},
        params={"reason": {"required": True}, "mode": {"options": ["fast", "slow"]}},
    )
    merged = merged_args(entry, {"reason": "patching", "mode": "fast"})
    assert merged == {"service": "nginx", "reason": "patching", "mode": "fast"}


def test_merged_args_requires_required_params():
    entry = _entry(params={"reason": {"required": True}})
    with pytest.raises(ValueError):
        merged_args(entry, {})


def test_merged_args_rejects_undeclared_values():
    entry = _entry(params={"reason": {"required": True}})
    with pytest.raises(ValueError, match="no param"):
        merged_args(entry, {"reason": "ok", "bogus": "nope"})


def test_merged_args_validates_choice_values():
    entry = _entry(params={"mode": {"options": ["fast", "slow"]}})
    with pytest.raises(ValueError):
        merged_args(entry, {"mode": "warp"})


def test_merged_args_with_no_params_and_no_values():
    entry = _entry(args={"service": "nginx"})
    assert merged_args(entry, None) == {"service": "nginx"}


def test_merged_args_optional_password_param_stays_optional():
    """A force-Open optional password param must NOT be treated as required.

    The editor writes ``required = false`` explicitly (argDefToParamDef) precisely
    so that runspec inference's "no default present ⇒ required" rule doesn't flip
    an optional password (which never carries a default) to required — otherwise
    the operator is forced to type a secret a selected credential was meant to
    supply. Leaving the value blank must be accepted.
    """
    entry = _entry(params={"token": {"type": "password", "required": False}})
    assert merged_args(entry, {}) == {}
    assert merged_args(entry, {"token": "hunter2"}) == {"token": "hunter2"}


def test_merged_args_password_param_without_required_flag_is_required():
    """Guards the inference rule the fix relies on: a password param with NO
    ``required`` key (and no default) infers required = true — which is exactly
    the pre-fix behavior the editor now avoids by pinning required = false."""
    entry = _entry(params={"token": {"type": "password"}})
    with pytest.raises(ValueError):
        merged_args(entry, {})


# ── registry I/O ──────────────────────────────────────────────────────────────


def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "runspec_console.config.saved_runs_path",
        lambda: tmp_path / "runspec_saved_runs.toml",
    )
    rows = [
        {
            "id": "restart-web",
            "title": "Restart web",
            "runnable": "service-restart",
            "group": "Production",
            "args": {"service": "nginx"},
            "params": {"reason": {"required": True}},
        },
        {"id": "disk-check", "runnable": "df", "host": "web01"},
    ]
    save_saved_runs(rows)
    assert load_saved_runs() == rows
    assert load_saved_run("disk-check") == rows[1]
    assert load_saved_run("nope") is None


def test_load_absent_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "runspec_console.config.saved_runs_path", lambda: tmp_path / "nope.toml"
    )
    assert load_saved_runs() == []


# ── Config Sync wiring ────────────────────────────────────────────────────────


def test_saved_runs_file_is_synced_and_merges_by_id():
    assert "runspec_saved_runs.toml" in gitsync.KNOWN_FILES
    assert configmerge.COLLECTION_FILES["runspec_saved_runs.toml"] == (
        "saved_run",
        "id",
    )


def test_module_reads_path_via_module_attribute(tmp_path, monkeypatch):
    # The tests (and Config Sync preserve logic) monkeypatch
    # runspec_console.config.saved_runs_path — savedruns must resolve it late.
    monkeypatch.setattr(
        "runspec_console.config.saved_runs_path", lambda: tmp_path / "s.toml"
    )
    save_saved_runs([{"id": "a", "runnable": "r", "host": "h"}])
    assert (tmp_path / "s.toml").exists()
    assert savedruns.load_saved_runs()[0]["id"] == "a"

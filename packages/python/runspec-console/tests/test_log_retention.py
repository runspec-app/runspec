"""
test_log_retention.py — Logs → Log cleanup…: reading, setting and running a
venv's log retention policy through its own `runspec logs retention`.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from runspec_console import provision as prov
from runspec_console.bridge import Bridge

RP = "/opt/venvs/ops/bin/runspec"


def _bridge():
    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        b = Bridge()
    b._ssh_pool = None
    b._hosts = [{"name": "prod-1", "ssh": "u@h1", "runspec_paths": [RP]}]
    return b


SHOW = {
    "venv": "/opt/venvs/ops",
    "file": "/opt/venvs/ops/runspec_logs_retention.toml",
    "policy": {"compact_after": "7d", "delete_after": "90d"},
    "last": {
        "ts": "2026-11-07T03:10:00Z",
        "compacted": 4,
        "deleted": 1,
        "freed_bytes": 9,
    },
}


def test_edit_argv():
    assert prov.retention_edit_argv(
        {"compact_after": "7D", "delete_after": "90d", "max_total_size": "5 gb"}
    ) == [
        "logs", "retention", "--compact-after", "7d", "--delete-after", "90d",
        "--max-total-size", "5GB", "--json",
    ]  # fmt: skip
    # a blank field is cleared, so the file ends up exactly as asked
    assert prov.retention_edit_argv({"delete_after": "30d"})[2:8] == [
        "--compact-after", "off", "--delete-after", "30d", "--max-total-size", "off",
    ]  # fmt: skip
    assert prov.retention_edit_argv({}) == ["logs", "retention", "--off", "--json"]
    assert prov.retention_edit_argv({"off": True, "delete_after": "9d"})[2] == "--off"


@pytest.mark.parametrize(
    "payload, msg",
    [
        ({"compact_after": "90d", "delete_after": "7d"}, "shorter"),
        ({"delete_after": "soon"}, "Not a duration"),
        ({"max_total_size": "lots"}, "Not a size"),
        ({"max_total_size": "5GB; rm -rf /"}, "Not a size"),
    ],
)
def test_edit_argv_refuses(payload, msg):
    with pytest.raises(prov.ProvisionError, match=msg):
        prov.retention_edit_argv(payload)


def test_read_is_read_only_as_login():
    b = _bridge()
    with patch.object(
        b, "_run_logs", return_value=(0, "noise\n" + json.dumps(SHOW), "")
    ) as m:
        out = b.logs_retention("prod-1", "ops")
    assert m.call_args.args[:3] == ("prod-1", RP, ["logs", "retention", "--json"])
    assert "run_as" not in m.call_args.kwargs
    assert (
        out["ok"] and out["policy"] == SHOW["policy"] and out["last"]["compacted"] == 4
    )


def test_read_on_an_old_runspec_says_upgrade():
    b = _bridge()
    with patch.object(
        b, "_run_logs", return_value=(1, "✗  No logs for 'retention'", "")
    ):
        out = b.logs_retention("prod-1", "ops")
    assert not out["ok"] and out["too_old"] and "0.80.0" in out["error"]


def test_home_and_unknown_venvs_are_refused():
    b = _bridge()
    assert not b.logs_retention("prod-1", "~/logs")["ok"]
    assert not b.logs_retention_set("prod-1", "nope", {"delete_after": "9d"})["ok"]


def test_set_runs_as_owner():
    b = _bridge()
    with patch.object(
        b, "_run_as_venv_owner", return_value=(0, json.dumps(SHOW), "", "svc")
    ) as m:
        out = b.logs_retention_set(
            "prod-1", "ops", {"compact_after": "7d", "delete_after": "90d"}
        )
    assert m.call_args.args[:3] == (
        "prod-1", RP,
        ["logs", "retention", "--compact-after", "7d", "--delete-after", "90d", "--max-total-size", "off", "--json"],
    )  # fmt: skip
    assert out == {
        "ok": True,
        "group": "ops",
        "file": SHOW["file"],
        "policy": SHOW["policy"],
        "last": SHOW["last"],
        "as_user": "svc",
    }


def test_set_off_and_bad_values():
    b = _bridge()
    removed = {"verb": "retention", "removed": True, "file": SHOW["file"]}
    with patch.object(
        b, "_run_as_venv_owner", return_value=(0, json.dumps(removed), "", "")
    ) as m:
        out = b.logs_retention_set("prod-1", "ops", {"off": True})
    assert m.call_args.args[2] == ["logs", "retention", "--off", "--json"]
    assert out["ok"] and out["policy"] is None and out["removed"]
    with patch.object(b, "_run_as_venv_owner") as m:
        out = b.logs_retention_set("prod-1", "ops", {"delete_after": "never"})
    assert not out["ok"] and not m.called


def test_preview_is_a_dry_run_as_login_and_apply_runs_as_owner():
    b = _bridge()
    res = {
        "verb": "retention",
        "pass": {"dry_run": True, "compacted": 2, "deleted": 3, "freed_bytes": 10},
    }
    with patch.object(b, "_run_logs", return_value=(0, json.dumps(res), "")) as m:
        out = b.logs_retention_run("prod-1", "ops")
    assert m.call_args.args[2] == ["logs", "retention", "--now", "--dry-run", "--json"]
    assert out["ok"] and out["compacted"] == 2 and out["dry_run"]
    res = {
        "verb": "retention",
        "pass": {"compacted": 2, "deleted": 3, "freed_bytes": 10},
    }
    with (
        patch.object(
            b, "_run_as_venv_owner", return_value=(0, json.dumps(res), "", "svc")
        ) as m,
        patch.object(b, "_invalidate_remote_history") as inv,
    ):
        out = b.logs_retention_run("prod-1", "ops", dry_run=False)
    assert m.call_args.args[2] == ["logs", "retention", "--now", "--json"]
    assert out["ok"] and out["as_user"] == "svc" and inv.called


def test_run_without_a_policy_is_reported():
    b = _bridge()
    res = {"verb": "retention", "pass": {"skipped": "no-policy"}}
    with patch.object(b, "_run_logs", return_value=(1, json.dumps(res), "")):
        out = b.logs_retention_run("prod-1", "ops")
    assert not out["ok"] and "no retention policy" in out["error"]

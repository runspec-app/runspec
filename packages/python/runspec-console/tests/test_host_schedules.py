"""
test_host_schedules.py — editing a venv's runspec-scheduler schedules from the
Schedules tab: the pure argv builder (host_schedules.py) and the bridge methods
that run `add` / `remove` / `sync` as the venv owner.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from runspec_console import host_schedules as hs
from runspec_console.bridge import Bridge

BASE = {"id": "nightly", "runnable": "backup", "cron": "0 2 * * *"}


# ── pure ────────────────────────────────────────────────────────────────────


def test_save_argv_minimal():
    sid, argv, env = hs.save_argv(BASE, backend="cron")
    assert sid == "nightly"
    assert argv == [
        "add",
        "--id",
        "nightly",
        "--runnable",
        "backup",
        "--cron",
        "0 2 * * *",
    ]
    assert env == {}


def test_save_argv_full_daemon():
    sid, argv, env = hs.save_argv(
        {
            "id": "sweep",
            "runnable": "filesystem",
            "command": "list-directory",
            "every": "15 m",
            "timezone": "Europe/London",
            "args": {"path": "/srv", "depth": 2, "empty": "", "none": None},
            "enabled": False,
            "timeout": "90m",
            "catch_up": True,
        },
        backend="daemon",
    )
    assert argv == [
        "add",
        "--id",
        "sweep",
        "--runnable",
        "filesystem",
        "--command",
        "list-directory",
        "--every",
        "15m",
        "--timezone",
        "Europe/London",
        "--args",
        json.dumps({"depth": 2, "path": "/srv"}, separators=(",", ":")),
        "--timeout",
        "90m",
        "--catch-up",
    ]
    assert env == {hs.ENABLED_ENV: "0"}


@pytest.mark.parametrize(
    ("over", "backend", "msg"),
    [
        ({"id": "bad id"}, "cron", "Schedule id"),
        ({"id": "../x"}, "cron", "Schedule id"),
        ({"runnable": ""}, "cron", "Pick a runnable"),
        ({"cron": ""}, "cron", "either a cron"),
        ({"every": "5m"}, "daemon", "either a cron"),
        ({"cron": "", "every": "5m"}, "cron", "Intervals need the scheduler daemon"),
        ({"cron": "", "every": "soon"}, "daemon", "Interval:"),
        ({"cron": "0 2 * *"}, "cron", "5 fields"),
        ({"cron": "@sometimes"}, "cron", "shortcut"),
        ({"timezone": "Europe/London; rm"}, "cron", "Timezone"),
        ({"timeout": "a while"}, "cron", "Timeout"),
        ({"catch_up": True}, "cron", "Catch-up"),
        ({"command": "list;rm"}, "cron", "Subcommand"),
        (
            {"args": {"token": "x"}, "password_args": ["token"]},
            "cron",
            "can't carry a password",
        ),
        ({"args": ["x"]}, "cron", "Arguments"),
    ],
)
def test_save_argv_refuses(over, backend, msg):
    with pytest.raises(hs.HostScheduleError, match=msg):
        hs.save_argv({**BASE, **over}, backend=backend)


def test_cron_shortcut_and_whitespace():
    assert hs.save_argv({**BASE, "cron": "@daily"}, backend="cron")[1][-1] == "@daily"
    assert (
        hs.save_argv({**BASE, "cron": " 0  2 * * * "}, backend="cron")[1][-1]
        == "0 2 * * *"
    )


def test_failure_text():
    assert (
        hs.failure(2, 'x\n{"error": "runnable \'b\' is not installed"}', "")
        == "runnable 'b' is not installed"
    )
    assert (
        hs.failure(
            1,
            json.dumps(
                {
                    "ok": False,
                    "errors": [
                        {"user": "svc", "error": "sudo: a password is required"}
                    ],
                }
            ),
            "",
        )
        == "svc: sudo: a password is required"
    )
    assert hs.failure(1, "", "boom\nlast line\n") == "last line"
    assert hs.failure(3, "", "") == "exit 3"


# ── bridge ──────────────────────────────────────────────────────────────────

REMOTE = {
    "name": "prod-1",
    "ssh": "u@h1",
    "runspec_paths": ["/opt/venvs/ops/bin/runspec"],
}


def _bridge(owner="svc"):
    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        b = Bridge()
    b._ssh_pool = None
    b._hosts = [REMOTE]
    b.venv_owner = lambda host, group: {"ok": True, "owner": owner, "self": "u"}
    b.audit = []
    b._append_tool_audit = b.audit.append
    return b


def _fake(replies):
    calls = []

    def run(host, binary, argv, owner, remote, env=None, timeout=45):
        calls.append({"binary": binary, "argv": argv, "owner": owner, "env": env})
        return replies.get(argv[0], (0, json.dumps({"ok": True}), ""))

    return calls, run


def _events(b):
    return [r["extra"]["event"] for r in b.audit]


def test_save_on_cron_adds_then_syncs_as_owner():
    b = _bridge()
    calls, run = _fake(
        {
            "sync": (
                0,
                json.dumps(
                    {
                        "ok": True,
                        "installed": [{"id": "nightly"}],
                        "skipped": [],
                        "users": ["svc"],
                    }
                ),
                "",
            )
        }
    )
    with patch.object(b, "_run_scheduler_as", side_effect=run):
        out = b.host_schedule_save(
            {"host": "prod-1", "venv": "ops", "backend": "cron", **BASE}
        )
    assert out["ok"] and out["warnings"] == []
    assert out["sync"] == {"installed": 1, "skipped": [], "users": ["svc"]}
    assert [c["argv"][0] for c in calls] == ["add", "sync"]
    assert {c["binary"] for c in calls} == {"/opt/venvs/ops/bin/runspec-scheduler"}
    assert {c["owner"] for c in calls} == {"svc"}
    assert _events(b) == ["schedule.host.saved"]


def test_save_on_daemon_does_not_sync_and_pauses_via_env():
    b = _bridge()
    calls, run = _fake({})
    with patch.object(b, "_run_scheduler_as", side_effect=run):
        out = b.host_schedule_save(
            {
                "host": "prod-1",
                "venv": "ops",
                "backend": "daemon",
                **BASE,
                "enabled": False,
            }
        )
    assert out["ok"] and "sync" not in out
    assert [c["argv"][0] for c in calls] == ["add"]
    assert calls[0]["env"] == {hs.ENABLED_ENV: "0"}


def test_rename_adds_new_then_removes_old():
    b = _bridge()
    calls, run = _fake({})
    with patch.object(b, "_run_scheduler_as", side_effect=run):
        out = b.host_schedule_save(
            {
                "host": "prod-1",
                "venv": "ops",
                "backend": "daemon",
                **BASE,
                "original_id": "old",
            }
        )
    assert out["ok"]
    assert [c["argv"] for c in calls][1] == ["remove", "--id", "old"]


def test_save_refused_by_scheduler_reports_its_error():
    b = _bridge()
    calls, run = _fake(
        {
            "add": (
                2,
                json.dumps(
                    {"error": "runnable 'backup' is not installed in this venv (ops)"}
                ),
                "",
            )
        }
    )
    with patch.object(b, "_run_scheduler_as", side_effect=run):
        out = b.host_schedule_save(
            {"host": "prod-1", "venv": "ops", "backend": "cron", **BASE}
        )
    assert not out["ok"] and "not installed" in out["error"]
    assert [c["argv"][0] for c in calls] == ["add"]  # no sync after a refused add
    assert _events(b) == ["schedule.host.save_failed"]


def test_invalid_payload_runs_nothing():
    b = _bridge()
    calls, run = _fake({})
    with patch.object(b, "_run_scheduler_as", side_effect=run):
        out = b.host_schedule_save(
            {"host": "prod-1", "venv": "ops", "backend": "cron", **BASE, "id": "a b"}
        )
    assert not out["ok"] and calls == []


def test_sync_failure_after_save_is_a_warning():
    b = _bridge()
    calls, run = _fake(
        {
            "sync": (
                1,
                json.dumps(
                    {
                        "ok": False,
                        "errors": [{"user": "svc", "error": "crontab: not allowed"}],
                    }
                ),
                "",
            )
        }
    )
    with patch.object(b, "_run_scheduler_as", side_effect=run):
        out = b.host_schedule_save(
            {"host": "prod-1", "venv": "ops", "backend": "cron", **BASE}
        )
    assert out["ok"]
    assert (
        "crontab wasn't updated" in out["warnings"][0]
        and "not allowed" in out["warnings"][0]
    )


def test_remove_then_sync():
    b = _bridge()
    calls, run = _fake({})
    with patch.object(b, "_run_scheduler_as", side_effect=run):
        out = b.host_schedule_remove(
            {"host": "prod-1", "venv": "ops", "backend": "cron", "id": "nightly"}
        )
    assert out["ok"]
    assert [c["argv"] for c in calls] == [["remove", "--id", "nightly"], ["sync"]]
    assert _events(b) == ["schedule.host.removed"]


def test_sync_reports_errors():
    b = _bridge()
    calls, run = _fake(
        {
            "sync": (
                1,
                json.dumps({"ok": False, "errors": [{"user": "svc", "error": "boom"}]}),
                "",
            )
        }
    )
    with patch.object(b, "_run_scheduler_as", side_effect=run):
        out = b.host_schedule_sync({"host": "prod-1", "venv": "ops"})
    assert not out["ok"] and "boom" in out["error"]


def test_unknown_venv_and_refused_sudo():
    b = _bridge()
    assert (
        "no venv web"
        in b.host_schedule_sync({"host": "prod-1", "venv": "web"})["error"]
    )
    with patch.object(
        b, "_run_scheduler_as", return_value=(1, "", "sudo: a password is required")
    ):
        out = b.host_schedule_remove(
            {"host": "prod-1", "venv": "ops", "backend": "cron", "id": "nightly"}
        )
    assert "passwordless sudo to 'svc'" in out["error"]


def test_login_owned_venv_runs_without_escalation():
    b = _bridge(owner="u")
    calls, run = _fake({})
    with patch.object(b, "_run_scheduler_as", side_effect=run):
        b.host_schedule_sync({"host": "prod-1", "venv": "ops"})
    assert calls[0]["owner"] == ""


# ── the daemon: settings + restart ──────────────────────────────────────────


def test_settings_argv():
    assert hs.settings_argv({"timeout": "6H", "catch_up_window": " 1d "}) == [
        "settings",
        "--timeout",
        "6h",
        "--catch-up-window",
        "1d",
    ]
    assert hs.settings_argv({"misfire_grace": 300}) == [
        "settings",
        "--misfire-grace",
        "300",
    ]
    with pytest.raises(hs.HostScheduleError, match="Nothing to change"):
        hs.settings_argv({"timeout": ""})
    with pytest.raises(hs.HostScheduleError, match="timeout must be"):
        hs.settings_argv({"timeout": "6 hours; rm -rf /"})


def test_too_old():
    assert hs.too_old("✗  Unknown command for 'runspec-scheduler': 'restart'")
    assert not hs.too_old("sudo: a password is required")


def test_settings_runs_as_owner_and_audits():
    b = _bridge()
    settings = {
        "backend": "daemon",
        "timeout": 7200,
        "misfire_grace": 300,
        "catch_up_window": 86400,
    }
    calls, run = _fake(
        {"settings": (0, json.dumps({"ok": True, "settings": settings}), "")}
    )
    with patch.object(b, "_run_scheduler_as", side_effect=run):
        out = b.host_scheduler_settings(
            {"host": "prod-1", "venv": "ops", "timeout": "2h"}
        )
    assert out == {"ok": True, "settings": settings}
    assert calls[0]["argv"] == ["settings", "--timeout", "2h"]
    assert calls[0]["owner"] == "svc"
    assert _events(b) == ["scheduler.host.settings"]
    assert b.audit[0]["extra"]["timeout"] == "2h"


def test_settings_refuses_bad_values_without_running():
    b = _bridge()
    calls, run = _fake({})
    with patch.object(b, "_run_scheduler_as", side_effect=run):
        out = b.host_scheduler_settings(
            {"host": "prod-1", "venv": "ops", "timeout": "soon"}
        )
    assert not out["ok"] and calls == []


def test_restart_reports_the_new_daemon():
    b = _bridge()
    reply = {
        "ok": True,
        "manager": "systemd-user",
        "warnings": ["w"],
        "daemon": {"state": "running", "pid": 77},
    }
    calls, run = _fake({"restart": (0, json.dumps(reply), "")})
    with patch.object(b, "_run_scheduler_as", side_effect=run):
        out = b.host_scheduler_restart({"host": "prod-1", "venv": "ops"})
    assert out == {
        "ok": True,
        "daemon": {"state": "running", "pid": 77},
        "manager": "systemd-user",
        "warnings": ["w"],
    }
    assert (
        calls[0]["argv"] == ["restart", "--wait", "15"] and calls[0]["owner"] == "svc"
    )
    assert _events(b) == ["scheduler.host.restarted"]


def test_restart_on_an_old_scheduler_says_upgrade():
    b = _bridge()
    with patch.object(
        b,
        "_run_scheduler_as",
        return_value=(2, "", "✗  Unknown command for 'runspec-scheduler': 'restart'"),
    ):
        out = b.host_scheduler_restart({"host": "prod-1", "venv": "ops"})
    assert not out["ok"] and "older than 0.7.0" in out["error"]
    assert _events(b) == ["scheduler.host.restart_failed"]

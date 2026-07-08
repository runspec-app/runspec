"""test_schedule_remote.py — the remote (host-run) schedule seam.

Covers the runner="host" routing added on top of the console-schedule CRUD:

  - get_schedules tags console schedules runner="console" and merges the shared
    host schedules (runner="host", with host/group/status),
  - create/update route a runner="host" payload to `runspec-scheduler add` over
    invoke_runnable (with cred-vars derived from the selected credentials),
  - delete / run_schedule_now of a remote id route to remove / run-now,
  - a console schedule still writes the local TOML with transient keys stripped.

Methods are bound onto a bare stub (à la test_schedule_bridge) to dodge
Bridge.__init__.
"""

from __future__ import annotations

import json
import threading
import types

from runspec_console.bridge import Bridge

_METHODS = (
    "_read_schedules",
    "get_schedules",
    "_schedule_next_run",
    "create_schedule",
    "update_schedule",
    "delete_schedule",
    "run_schedule_now",
    "_strip_transient",
    "_scheduler_targets",
    "_run_scheduler_list",
    "_refresh_remote_schedules",
    "_remote_schedules",
    "_host_has_scheduler",
    "_remote_schedule_lookup",
    "_target_group",
    "_cred_var_names",
    "_invoke_scheduler_sync",
    "_remote_add_args",
    "_remote_create",
    "_remote_delete",
    "_record_schedule_fire",
    "_invoke_scheduled",
    "_on_schedule_event",
)


def _stub(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "runspec_console.config.hosts_path", lambda: tmp_path / "runspec_hosts.toml"
    )
    ns = types.SimpleNamespace()
    for name in _METHODS:
        setattr(ns, name, types.MethodType(getattr(Bridge, name), ns))
    ns._lock = threading.Lock()
    ns.restart_triggers = lambda: None
    ns._runnables_cache = []
    ns._remote_sched_cache = {}
    ns._ssh_pool = None
    ns.get_config = lambda: {}
    ns.dispatched = []
    ns._dispatch = lambda ev, payload: ns.dispatched.append((ev, payload))
    # The venv name of the runspec path must equal the scheduler runnable's group
    # ("ops") — venv_name(path) = path.parent.parent.name.
    ns._host_entry = lambda h: {
        "name": h,
        "ssh": f"user@{h}",
        "runspec_paths": ["/opt/ops/bin/runspec"],
    }
    ns._credential_entry = lambda cid: {
        "jira": {"label": "Jira", "kind": "userpass"}
    }.get(cid)
    # invoke_runnable: simulate a completed run by driving on_progress synchronously.
    ns.calls = []

    def fake_invoke(host, runnable, args, **kw):
        ns.calls.append({"host": host, "runnable": runnable, "args": args, **kw})
        cb = kw.get("on_progress")
        if cb:
            cb(
                "line",
                {
                    "line": json.dumps(
                        {"id": args.get("id"), "runnable": "backup", "exit_code": 0}
                    )
                },
            )
            cb("done", {"exit_code": 0})
        return "inv-1"

    ns.invoke_runnable = fake_invoke
    return ns


def _with_scheduler(ns, host="h1", group="ops"):
    ns._runnables_cache = [
        {"name": "runspec-scheduler", "host": host, "group": group},
        {"name": "backup", "host": host, "group": group, "runAs": "svc"},
    ]


def test_get_schedules_merges_console_and_remote(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    from runspec_console.bridge_utils import _write_schedules

    _write_schedules(
        tmp_path / "runspec_schedules.toml",
        [
            {
                "id": "local1",
                "action": "runnable",
                "host": "h1",
                "runnable": "x",
                "when": {"every": "5m"},
            }
        ],
    )
    ns._remote_sched_cache = {
        ("h1", "ops"): {
            "status": "ok",
            "schedules": [
                {
                    "id": "rem1",
                    "runnable": "backup",
                    "enabled": True,
                    "when": {"cron": "0 2 * * *"},
                }
            ],
        }
    }
    out = ns.get_schedules()
    by_id = {s["id"]: s for s in out}
    assert by_id["local1"]["runner"] == "console"
    assert by_id["rem1"]["runner"] == "host"
    assert by_id["rem1"]["host"] == "h1" and by_id["rem1"]["group"] == "ops"
    assert by_id["rem1"]["status"] == "ok"


def test_create_remote_routes_to_add_with_cred_vars(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _with_scheduler(ns)
    ns.create_schedule(
        {
            "id": "rem1",
            "runner": "host",
            "host": "h1",
            "runnable": "backup",
            "when": {"cron": "0 2 * * *"},
            "args": {"level": "warn"},
            "enabled": True,
            "credential_ids": ["jira"],
        }
    )
    assert len(ns.calls) == 1
    call = ns.calls[0]
    assert call["runnable"] == "runspec-scheduler"
    assert call["command_path"] == ["add"]
    assert call["group"] == "ops"
    assert call["credential_ids"] == ["jira"]
    a = call["args"]
    assert a["id"] == "rem1" and a["runnable"] == "backup" and a["cron"] == "0 2 * * *"
    assert json.loads(a["args"]) == {"level": "warn"}
    # cred-vars carries the derived NAMES (not secrets): Jira userpass → USER(NAME)/PASSWORD
    names = json.loads(a["cred-vars"])
    assert "JIRA_USERNAME" in names and "JIRA_PASSWORD" in names
    # Nothing was written to the local console schedules file.
    assert ns._read_schedules() == []


def test_create_console_writes_local_stripped(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    ns.create_schedule(
        {
            "id": "c1",
            "runner": "console",
            "action": "runnable",
            "host": "h1",
            "runnable": "x",
            "when": {"every": "5m"},
            "status": "ok",
            "credential_ids": ["jira"],
        }
    )
    assert ns.calls == []  # no remote invoke
    saved = ns._read_schedules()[0]
    assert saved["id"] == "c1"
    # transient/routing keys are not persisted
    assert (
        "runner" not in saved
        and "status" not in saved
        and "credential_ids" not in saved
    )


def test_delete_remote_routes_to_remove(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _with_scheduler(ns)
    ns._remote_sched_cache = {
        ("h1", "ops"): {"status": "ok", "schedules": [{"id": "rem1"}]}
    }
    ns.delete_schedule("rem1")
    assert ns.calls[0]["command_path"] == ["remove"]
    assert ns.calls[0]["args"] == {"id": "rem1"}
    assert ns.calls[0]["host"] == "h1"


def test_run_now_remote_routes_to_run_now(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _with_scheduler(ns)
    ns._remote_sched_cache = {
        ("h1", "ops"): {"status": "ok", "schedules": [{"id": "rem1"}]}
    }
    result = ns.run_schedule_now("rem1")
    assert ns.calls[0]["command_path"] == ["run-now"]
    assert result["runner"] == "host" and result["host"] == "h1"
    assert result["exit_code"] == 0


def test_host_has_scheduler_and_target_group(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _with_scheduler(ns)
    assert ns._host_has_scheduler("h1", "ops") is True
    assert ns._host_has_scheduler("h1", "other") is False
    assert ns._target_group("h1", "backup") == "ops"


def test_refresh_remote_schedules(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _with_scheduler(ns)
    monkeypatch.setattr(
        ns, "_run_scheduler_list", lambda t: [{"id": "rem1", "runnable": "backup"}]
    )
    ns._refresh_remote_schedules()
    assert ns._remote_sched_cache[("h1", "ops")]["status"] == "ok"
    assert ns._remote_sched_cache[("h1", "ops")]["schedules"][0]["id"] == "rem1"


def test_refresh_remote_unreachable_keeps_last(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    _with_scheduler(ns)
    ns._remote_sched_cache = {
        ("h1", "ops"): {"status": "ok", "schedules": [{"id": "old"}]}
    }

    def boom(t):
        raise RuntimeError("down")

    monkeypatch.setattr(ns, "_run_scheduler_list", boom)
    ns._refresh_remote_schedules()
    info = ns._remote_sched_cache[("h1", "ops")]
    assert info["status"] == "unreachable"
    assert info["schedules"][0]["id"] == "old"  # last-known kept

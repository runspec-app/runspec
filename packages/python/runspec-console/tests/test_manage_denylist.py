"""
test_manage_denylist.py — the built-in `manage_denylist` agent tool.

Binds the real Bridge methods onto a light stub (no Bridge.__init__). `_ssh_run`
is faked to record the commands the tool would run, so we assert on command
construction, host/venv fan-out, target filtering, and the autonomy gate.
"""

from __future__ import annotations

import json
from typing import Any

from runspec_console.bridge import Bridge

HOSTS = [
    {
        "name": "prod-1",
        "ssh": "deploy@prod-1",
        "identityFile": "~/.ssh/id",
        "runspec_paths": ["/opt/venvs/app/bin/runspec", "/opt/venvs/ops/bin/runspec"],
    },
    {
        "name": "prod-2",
        "ssh": "deploy@prod-2",
        "runspec_paths": ["/opt/venvs/app/bin/runspec"],
    },
    {"name": "nopath", "ssh": "x@y"},  # no runspec path → excluded
]


class _StubBridge:
    def __init__(self, hosts: list[dict[str, Any]]):
        self._hosts = hosts
        self.ssh_calls: list[tuple[str, str, Any]] = []

    def _ssh_run(
        self, ssh: str, command: str, *, identity_file: Any = None, timeout: int = 15
    ):
        self.ssh_calls.append((ssh, command, identity_file))
        return (0, "ok", "")

    _denylist_tool = Bridge._denylist_tool
    _run_manage_denylist = Bridge._run_manage_denylist
    _run_custom_tool = Bridge._run_custom_tool
    _effective_tool_autonomy = Bridge._effective_tool_autonomy
    denylist_status = Bridge.denylist_status


# ── tool schema ────────────────────────────────────────────────────────────


def test_autonomy_list_is_autonomous_mutations_confirm():
    b = _StubBridge(HOSTS)
    # read-only list runs without a prompt so the agent can stay aware
    assert (
        b._effective_tool_autonomy("manage_denylist", {"action": "list"})
        == "autonomous"
    )
    # mutations stay gated
    assert b._effective_tool_autonomy("manage_denylist", {"action": "all"}) == "confirm"
    assert (
        b._effective_tool_autonomy("manage_denylist", {"action": "clear"}) == "confirm"
    )
    assert b._effective_tool_autonomy("manage_denylist", {}) == "confirm"


def test_tool_absent_without_runspec_paths():
    assert _StubBridge([{"name": "x", "ssh": "x@y"}])._denylist_tool() is None
    assert _StubBridge([])._denylist_tool() is None


def test_tool_targets_enum_lists_hosts_with_paths():
    tool = _StubBridge(HOSTS)._denylist_tool()
    assert tool is not None
    enum = tool["input_schema"]["properties"]["targets"]["items"]["enum"]
    assert enum == ["prod-1", "prod-2"]  # nopath excluded
    assert tool["input_schema"]["required"] == ["action"]


# ── command construction & fan-out ─────────────────────────────────────────


def test_all_runs_on_every_venv():
    b = _StubBridge(HOSTS)
    out = b._run_manage_denylist({"action": "all", "reason": "freeze"})
    # 2 venvs on prod-1 + 1 on prod-2 = 3 calls; nopath skipped
    assert len(b.ssh_calls) == 3
    assert all(c[1].endswith("deny all --reason freeze") for c in b.ssh_calls)
    assert (
        "deploy@prod-1",
        "/opt/venvs/app/bin/runspec deny all --reason freeze",
        "~/.ssh/id",
    ) in b.ssh_calls
    assert "3/3 venv(s) ok across 2 host(s)" in out


def test_add_builds_glob_flags():
    b = _StubBridge(HOSTS)
    b._run_manage_denylist(
        {"action": "add", "runnable": ["deploy", "restart-*"], "user": ["svc"]}
    )
    cmd = b.ssh_calls[0][1]
    assert "deny add -r deploy -r 'restart-*' -u svc" in cmd


def test_targets_filter_restricts_hosts():
    b = _StubBridge(HOSTS)
    b._run_manage_denylist({"action": "clear", "targets": ["prod-2"]})
    assert [c[0] for c in b.ssh_calls] == ["deploy@prod-2"]
    assert b.ssh_calls[0][1].endswith("deny clear")


def test_mode_requires_setting():
    b = _StubBridge(HOSTS)
    out = b._run_manage_denylist({"action": "mode"})
    assert out.startswith("Error:") and "setting" in out
    assert b.ssh_calls == []


def test_mode_sets_level():
    b = _StubBridge(HOSTS)
    b._run_manage_denylist({"action": "mode", "setting": "warn", "targets": ["prod-2"]})
    assert b.ssh_calls[0][1].endswith("deny mode warn")


def test_invalid_action_errors():
    b = _StubBridge(HOSTS)
    out = b._run_manage_denylist({"action": "nuke"})
    assert out.startswith("Error:")
    assert b.ssh_calls == []


def test_no_matching_target_errors():
    b = _StubBridge(HOSTS)
    out = b._run_manage_denylist({"action": "list", "targets": ["ghost"]})
    assert "no matching" in out.lower()
    assert b.ssh_calls == []


def test_string_targets_coerced_to_list():
    b = _StubBridge(HOSTS)
    b._run_manage_denylist({"action": "list", "targets": "prod-2"})
    assert [c[0] for c in b.ssh_calls] == ["deploy@prod-2"]


def test_dispatch_through_run_custom_tool():
    b = _StubBridge(HOSTS)
    out = b._run_custom_tool(
        "manage_denylist", {"action": "list", "targets": ["prod-2"]}
    )
    assert "deny list" in out
    assert len(b.ssh_calls) == 1


def test_failure_reported_per_venv():
    b = _StubBridge(HOSTS)

    def failing(ssh, command, *, identity_file=None, timeout=15):
        b.ssh_calls.append((ssh, command, identity_file))
        return (1, "", "boom")

    b._ssh_run = failing  # type: ignore[method-assign]
    out = b._run_manage_denylist({"action": "all", "targets": ["prod-2"]})
    assert "✗ prod-2/app: boom" in out
    assert "0/1 venv(s) ok" in out


# ── denylist_status (structured) ────────────────────────────────────────────

_STATUS_JSON = json.dumps(
    {
        "path": "/opt/venvs/app/runspec_denylist.toml",
        "present": True,
        "mode": "enforce",
        "disabled": False,
        "reason": "",
        "rules": [
            {
                "runnables": ["deploy"],
                "users": ["*"],
                "hosts": ["*"],
                "reason": "freeze",
            }
        ],
    }
)


def test_status_parses_json_per_venv():
    b = _StubBridge(HOSTS)
    b._ssh_run = lambda ssh, command, *, identity_file=None, timeout=15: (
        0,
        _STATUS_JSON,
        "",
    )  # type: ignore[method-assign]
    rows = b.denylist_status(["prod-2"])
    assert len(rows) == 1
    row = rows[0]
    assert row["host"] == "prod-2" and row["venv"] == "app"
    assert row["ok"] is True and row["present"] is True
    assert row["rules"][0]["runnables"] == ["deploy"]


def test_status_all_hosts_one_row_per_venv():
    b = _StubBridge(HOSTS)
    b._ssh_run = lambda ssh, command, *, identity_file=None, timeout=15: (
        0,
        _STATUS_JSON,
        "",
    )  # type: ignore[method-assign]
    rows = b.denylist_status()
    # 2 venvs on prod-1 + 1 on prod-2 = 3 rows; nopath excluded
    assert [(r["host"], r["venv"]) for r in rows] == [
        ("prod-1", "app"),
        ("prod-1", "ops"),
        ("prod-2", "app"),
    ]
    assert all("deny list --json" in c[1] for c in b.ssh_calls)


def test_status_error_surfaced_as_row():
    b = _StubBridge(HOSTS)
    b._ssh_run = lambda ssh, command, *, identity_file=None, timeout=15: (
        255,
        "",
        "ssh down",
    )  # type: ignore[method-assign]
    rows = b.denylist_status(["prod-2"])
    assert rows[0]["ok"] is False
    assert rows[0]["error"] == "ssh down"

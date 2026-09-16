"""
test_manage_denylist.py — the built-in `manage_denylist` agent tool.

Binds the real Bridge methods onto a light stub (no Bridge.__init__). `_ssh_run`
is faked to record the commands the tool would run, so we assert on command
construction, host/venv fan-out, target filtering, and the autonomy gate.
"""

from __future__ import annotations

import json
import os
import shutil
import stat as _stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from runspec_console import bridge_utils as bu
from runspec_console.bridge import Bridge
from runspec_console.bridge_const import ToolCancelled

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

    def get_config(self) -> dict[str, Any]:
        return getattr(self, "_cfg", {})

    _fanout_workers = Bridge._fanout_workers
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


# ── parallel fan-out (bounded, order-preserving, cancellable) ───────────────


def _slow_ssh_run(b: _StubBridge, slow_target: str, delay: float = 0.05):
    def run(ssh, command, *, identity_file=None, timeout=15):
        if ssh == slow_target:
            time.sleep(delay)
        b.ssh_calls.append((ssh, command, identity_file))
        return (0, _STATUS_JSON, "")

    return run


def test_status_rows_keep_order_under_parallelism():
    b = _StubBridge(HOSTS)
    b._ssh_run = _slow_ssh_run(b, "deploy@prod-1")  # type: ignore[method-assign]
    rows = b.denylist_status()
    assert [(r["host"], r["venv"]) for r in rows] == [
        ("prod-1", "app"),
        ("prod-1", "ops"),
        ("prod-2", "app"),
    ]


def _concurrency_probe(b: _StubBridge, hold: float = 0.05):
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def run(ssh, command, *, identity_file=None, timeout=15):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(hold)
        with lock:
            state["active"] -= 1
        b.ssh_calls.append((ssh, command, identity_file))
        return (0, "ok", "")

    return run, state


def test_fanout_runs_concurrently():
    b = _StubBridge(HOSTS)
    run, state = _concurrency_probe(b)
    b._ssh_run = run  # type: ignore[method-assign]
    out = b._run_manage_denylist({"action": "all"})
    assert state["peak"] >= 2  # not serial any more
    assert "3/3 venv(s) ok across 2 host(s)" in out
    # Summary lines stay in host/venv order.
    body = out.splitlines()[1:]
    assert [ln.split(":")[0] for ln in body] == [
        "✓ prod-1/app",
        "✓ prod-1/ops",
        "✓ prod-2/app",
    ]


def test_fanout_workers_honours_refresh_workers():
    b = _StubBridge(HOSTS)
    b._cfg = {"refresh": {"workers": 1}}
    run, state = _concurrency_probe(b)
    b._ssh_run = run  # type: ignore[method-assign]
    b.denylist_status()
    assert state["peak"] == 1
    assert b._fanout_workers() == 1


def test_on_progress_lines_are_ordered():
    b = _StubBridge(HOSTS)
    b._ssh_run = _slow_ssh_run(b, "deploy@prod-1")  # type: ignore[method-assign]
    seen: list[str] = []
    b._run_manage_denylist({"action": "list"}, on_progress=seen.append)
    assert seen == ["host 1 of 2: prod-1", "host 2 of 2: prod-2"]


def test_cancel_event_raises_tool_cancelled_and_stops_issuing():
    b = _StubBridge(HOSTS)
    b._cfg = {"refresh": {"workers": 1}}  # serial so the cancel lands mid-run
    ev = threading.Event()

    def run(ssh, command, *, identity_file=None, timeout=15):
        b.ssh_calls.append((ssh, command, identity_file))
        ev.set()  # cancel from inside the first command
        return (0, "ok", "")

    b._ssh_run = run  # type: ignore[method-assign]
    with pytest.raises(ToolCancelled):
        b._run_manage_denylist({"action": "all"}, cancel_event=ev)
    assert len(b.ssh_calls) < 3  # later venvs were never issued


def test_cancel_before_start_issues_nothing():
    b = _StubBridge(HOSTS)
    ev = threading.Event()
    ev.set()
    with pytest.raises(ToolCancelled):
        b._run_manage_denylist({"action": "all"}, cancel_event=ev)
    assert b.ssh_calls == []


# ── escalation: a venv root owned by a service account ──────────────────────
#
# `runspec deny` writes into the venv root; a venv created as a service account
# (`sudo -u svc python -m venv …`) refuses the login user's write. The fan-out
# re-runs a permission-denied *write* once, escalated to the venv owner / root.

_DENIED_OLD_CORE = (
    "Traceback (most recent call last):\n  ...\n"
    "PermissionError: [Errno 13] Permission denied: "
    "'/opt/venvs/app/runspec_denylist.toml.tmp'"
)
_DENIED_NEW_CORE = (
    "✗  Cannot write the denylist: permission denied.\n"
    "   /opt/venvs/app/runspec_denylist.toml\n"
    "   The venv is owned by 'svc-app' — re-run as that user"
)


def _denied_then_escalated(
    b: _StubBridge,
    denied: str,
    escalated=(0, "=====AS svc-app\n✓  Disabled ALL runnables in this venv.", ""),
):
    def run(ssh, command, *, identity_file=None, timeout=15):
        b.ssh_calls.append((ssh, command, identity_file))
        if "sudo -n" in command:
            return escalated
        return (1, "", denied)

    return run


@pytest.mark.parametrize("denied", [_DENIED_OLD_CORE, _DENIED_NEW_CORE])
def test_permission_denied_write_is_retried_escalated(denied: str):
    b = _StubBridge(HOSTS)
    b._ssh_run = _denied_then_escalated(b, denied)  # type: ignore[method-assign]
    out = b._run_manage_denylist(
        {"action": "all", "reason": "freeze", "targets": ["prod-2"]}
    )
    assert len(b.ssh_calls) == 2
    plain, escalated = b.ssh_calls[0][1], b.ssh_calls[1][1]
    assert plain == "/opt/venvs/app/bin/runspec deny all --reason freeze"
    assert escalated == bu.denylist_escalation_cmd(
        "/opt/venvs/app/bin/runspec", ["all", "--reason", "freeze"]
    )
    assert 'sudo -n -u "$o" "$rp" deny all --reason freeze' in escalated
    assert "✓ prod-2/app (as svc-app): ✓  Disabled ALL runnables" in out
    assert "1/1 venv(s) ok" in out


def test_escalation_root_tier_labelled():
    b = _StubBridge(HOSTS)
    b._ssh_run = _denied_then_escalated(  # type: ignore[method-assign]
        b, _DENIED_NEW_CORE, (0, "=====AS root\n✓  Cleared denylist", "")
    )
    out = b._run_manage_denylist({"action": "clear", "targets": ["prod-2"]})
    assert "✓ prod-2/app (as root): ✓  Cleared denylist" in out


def test_escalation_refused_is_reported_per_venv():
    b = _StubBridge(HOSTS)
    refusal = "cannot escalate: the venv root /opt/venvs/app is owned by svc-app and passwordless sudo (as svc-app or root) for /opt/venvs/app/bin/runspec was refused"
    b._ssh_run = _denied_then_escalated(  # type: ignore[method-assign]
        b, _DENIED_NEW_CORE, (77, "", refusal)
    )
    out = b._run_manage_denylist(
        {"action": "mode", "setting": "warn", "targets": ["prod-2"]}
    )
    assert len(b.ssh_calls) == 2
    assert f"✗ prod-2/app: {refusal}" in out
    assert "0/1 venv(s) ok" in out


def test_list_never_escalates():
    b = _StubBridge(HOSTS)
    b._ssh_run = _denied_then_escalated(b, _DENIED_NEW_CORE)  # type: ignore[method-assign]
    out = b._run_manage_denylist({"action": "list", "targets": ["prod-2"]})
    assert len(b.ssh_calls) == 1
    assert "✗ prod-2/app:" in out


def test_non_permission_failure_not_retried():
    b = _StubBridge(HOSTS)

    def failing(ssh, command, *, identity_file=None, timeout=15):
        b.ssh_calls.append((ssh, command, identity_file))
        return (1, "", "✗  mode requires one of: enforce | warn | off")

    b._ssh_run = failing  # type: ignore[method-assign]
    b._run_manage_denylist({"action": "all", "targets": ["prod-2"]})
    assert len(b.ssh_calls) == 1


def test_successful_plain_write_not_retried():
    b = _StubBridge(HOSTS)
    out = b._run_manage_denylist({"action": "all", "targets": ["prod-2"]})
    assert len(b.ssh_calls) == 1
    assert "(as " not in out


# ── pure helpers ────────────────────────────────────────────────────────────


def test_looks_permission_denied():
    assert bu.looks_permission_denied(_DENIED_OLD_CORE)
    assert bu.looks_permission_denied(_DENIED_NEW_CORE)
    assert bu.looks_permission_denied("OSError: [Errno 13] Permission denied")
    assert not bu.looks_permission_denied("✗  Unknown deny action: frob")
    assert not bu.looks_permission_denied("")


def test_split_denylist_as():
    assert bu.split_denylist_as("=====AS svc\n✓ ok\nmore") == ("svc", "✓ ok\nmore")
    assert bu.split_denylist_as("=====AS root") == ("root", "")
    assert bu.split_denylist_as("✓ ok") == ("", "✓ ok")
    assert bu.split_denylist_as("") == ("", "")


def test_escalation_cmd_quotes_path_and_args():
    cmd = bu.denylist_escalation_cmd(
        "/opt/v x/bin/runspec", ["add", "-r", "deploy", "--reason", "change freeze"]
    )
    assert cmd.startswith("rp='/opt/v x/bin/runspec'; ")
    assert cmd.count("deny add -r deploy --reason 'change freeze'") == 3
    assert 'stat -c %U "$r"' in cmd and "exit 77" in cmd
    assert cmd.count("=====AS ") == 2


# ── the real shell script with a fake sudo / stat / runspec on PATH ────────

_SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(not _SH, reason="needs sh")


def _fake_tools(
    tmp_path: Path, *, owner: str, sudo_allows: str
) -> tuple[str, Path, Path]:
    """Build a fake venv (`bin/runspec` shell script) plus a PATH dir holding a
    fake `sudo` (records `-u <user>`, honours `sudo_allows` = 'owner'|'root'|
    'both'|'none', then execs the command) and a fake `stat` reporting `owner`.
    Returns (runspec_path, bin_dir, sudo_log)."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    rp = venv / "bin" / "runspec"
    rp.write_text(
        '#!/bin/sh\nif [ "$2" = "list" ]; then echo listed; exit 0; fi\n'
        'echo "wrote: $*"; exit 0\n'
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "sudo.log"
    sudo = tools / "sudo"
    sudo.write_text(
        "#!/bin/sh\n"
        "as=root; tag=default\n"
        'while [ $# -gt 0 ]; do case "$1" in -n) shift;; -u) as=$2; tag="-u $2"; shift 2;; *) break;; esac; done\n'
        f'echo "$tag $*" >> "{log}"\n'
        f'allow="{sudo_allows}"\n'
        'if [ "$as" = root ]; then [ "$allow" = root ] || [ "$allow" = both ] || { echo "sudo: a password is required" >&2; exit 1; }\n'
        'else [ "$allow" = owner ] || [ "$allow" = both ] || { echo "sudo: a password is required" >&2; exit 1; }; fi\n'
        'exec "$@"\n'
    )
    fake_stat = tools / "stat"
    fake_stat.write_text(f'#!/bin/sh\necho "{owner}"\n')
    for f in (rp, sudo, fake_stat):
        f.chmod(f.stat().st_mode | _stat.S_IXUSR)
    return str(rp), tools, log


def _run_escalation(tmp_path: Path, *, owner: str, sudo_allows: str):
    rp, tools, log = _fake_tools(tmp_path, owner=owner, sudo_allows=sudo_allows)
    env = dict(os.environ, PATH=f"{tools}{os.pathsep}{os.environ.get('PATH', '')}")
    proc = subprocess.run(
        [_SH, "-c", bu.denylist_escalation_cmd(rp, ["all", "--reason", "x y"])],
        capture_output=True,
        text=True,
        env=env,
    )
    return proc, (log.read_text().splitlines() if log.exists() else [])


@needs_sh
def test_sh_owner_tier_when_owner_differs_and_sudo_u_allowed(tmp_path: Path):
    proc, log = _run_escalation(tmp_path, owner="svc-app", sudo_allows="owner")
    assert proc.returncode == 0, proc.stderr
    as_user, rest = bu.split_denylist_as(proc.stdout)
    assert as_user == "svc-app"
    assert rest.strip() == "wrote: deny all --reason x y"
    assert log[-1].startswith("-u svc-app ")


@needs_sh
def test_sh_root_tier_when_owner_grant_missing(tmp_path: Path):
    """The `scoped` sudoers drop-in grants (root) on the venv bin dir only."""
    proc, log = _run_escalation(tmp_path, owner="svc-app", sudo_allows="root")
    assert proc.returncode == 0, proc.stderr
    as_user, rest = bu.split_denylist_as(proc.stdout)
    assert as_user == "root"
    assert rest.strip() == "wrote: deny all --reason x y"
    assert log[0].startswith("-u svc-app ") and log[-1].startswith("default ")


@needs_sh
def test_sh_root_tier_when_owner_is_login_user(tmp_path: Path):
    me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    proc, log = _run_escalation(tmp_path, owner=me, sudo_allows="both")
    assert proc.returncode == 0, proc.stderr
    assert bu.split_denylist_as(proc.stdout)[0] == "root"
    assert all(line.startswith("default ") for line in log)  # owner tier skipped


@needs_sh
def test_sh_refusal_exits_77_with_hint(tmp_path: Path):
    proc, _log = _run_escalation(tmp_path, owner="svc-app", sudo_allows="none")
    assert proc.returncode == 77
    assert proc.stdout == ""
    assert "cannot escalate" in proc.stderr
    assert "owned by svc-app" in proc.stderr
    assert "sudo -u svc-app" in proc.stderr and "deny all --reason 'x y'" in proc.stderr

"""
test_jump_transport.py — the system-ssh jump transport for legacy hosts.

A host entry with a ``jump`` key is reached over the system ``ssh`` binary
(``-J``), not paramiko, so ``~/.ssh/config`` legacy algorithms apply (the
CentOS 6 ssh-rsa/ssh-dss case that paramiko 4+/5 can't negotiate). Covers the
argv builder, the ``ssh_jump_run`` runner, the ``run_remote`` jump branch,
editor validation + TOML round-trip, and bridge routing for discovery and
connectivity.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from runspec_console import executor
from runspec_console.executor import (
    _system_ssh_argv,
    run_remote,
    ssh_jump_run,
)
from runspec_console.hosts import load_hosts, save_hosts, validate_editor_hosts


# ── _system_ssh_argv ──────────────────────────────────────────────────────────


def test_system_ssh_argv_basic_jump():
    argv = _system_ssh_argv("svc@c6", "bastion")
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert argv[argv.index("-J") + 1] == "bastion"
    assert argv[-1] == "svc@c6"  # target last, user@host reassembled


def test_system_ssh_argv_identity_port_and_timeout():
    argv = _system_ssh_argv(
        "user@host:2222",
        "jump",
        identity_file="/keys/id_rsa",
        global_ssh_config={"connect_timeout": 7},
    )
    assert argv[argv.index("-i") + 1].endswith("id_rsa")
    assert argv[argv.index("-p") + 1] == "2222"
    assert "ConnectTimeout=7" in argv
    # port carried via -p, not embedded in the target
    assert argv[-1] == "user@host"


def test_system_ssh_argv_global_user_and_key_fallback():
    argv = _system_ssh_argv(
        "host", "jump", global_ssh_config={"user": "deploy", "identityFile": "/g/k"}
    )
    assert argv[-1] == "deploy@host"
    assert argv[argv.index("-i") + 1].endswith("k")


def test_system_ssh_argv_bad_timeout_falls_back():
    argv = _system_ssh_argv("h", "j", global_ssh_config={"connect_timeout": "nope"})
    assert any(a.startswith("ConnectTimeout=") for a in argv)


# ── ssh_jump_run ──────────────────────────────────────────────────────────────


class _Proc:
    def __init__(self, code=0, out="", err=""):
        self.returncode = code
        self.stdout = out
        self.stderr = err


def test_ssh_jump_run_invokes_system_ssh_and_returns_triple():
    with patch.object(
        executor.subprocess, "run", return_value=_Proc(0, "hello", "")
    ) as m:
        code, out, err = ssh_jump_run("svc@c6", "runspec local", jump="bastion")
    assert (code, out, err) == (0, "hello", "")
    argv = m.call_args.args[0]
    assert argv[0] == "ssh"
    assert argv[-1] == "runspec local"  # command is the final argv element
    assert "bastion" in argv


def test_ssh_jump_run_missing_ssh_binary():
    with patch.object(executor.subprocess, "run", side_effect=FileNotFoundError()):
        code, _out, err = ssh_jump_run("h", "true", jump="j")
    assert code == -1 and "OpenSSH" in err


def test_ssh_jump_run_timeout():
    exc = subprocess.TimeoutExpired(cmd="ssh", timeout=15)
    with patch.object(executor.subprocess, "run", side_effect=exc):
        code, _out, err = ssh_jump_run("h", "true", jump="j", timeout=15)
    assert code == -1 and "timed out" in err


# ── run_remote jump branch ────────────────────────────────────────────────────


def test_run_remote_jump_streams_over_system_ssh_not_paramiko(monkeypatch):
    captured: dict = {}

    def fake_stream(cmd, on_line, on_done, **kw):
        captured["cmd"] = cmd
        on_done(0, 5)

    def boom(*a, **k):
        raise AssertionError("paramiko path must not be used for a jump host")

    monkeypatch.setattr(executor, "_stream", fake_stream)
    monkeypatch.setattr(executor, "_make_ssh_client", boom)

    done: dict = {}
    run_remote(
        "svc@c6",
        "/opt/bin/runspec",
        "disk-usage",
        {},
        [],
        on_line=lambda *_a: None,
        on_done=lambda code, _ms: done.update(code=code),
        jump="bastion",
    )
    assert done["code"] == 0
    cmd = captured["cmd"]
    assert cmd[0] == "ssh" and "bastion" in cmd
    # The last element is the remote command string that invokes the runnable.
    assert "disk-usage" in cmd[-1]


# ── editor validation ─────────────────────────────────────────────────────────


def test_validate_accepts_jump_with_target():
    assert (
        validate_editor_hosts([{"name": "c6", "ssh": "svc@c6", "jump": "bastion"}])
        == []
    )
    # hostname (editor shape) also counts as a target
    assert validate_editor_hosts([{"name": "c6", "hostname": "c6", "jump": "b"}]) == []


def test_validate_rejects_jump_without_target():
    errs = validate_editor_hosts([{"name": "c6", "jump": "bastion"}])
    assert any("needs an ssh/hostname" in e for e in errs)


def test_validate_rejects_blank_jump_string():
    errs = validate_editor_hosts([{"name": "c6", "ssh": "svc@c6", "jump": "   "}])
    assert any("non-empty" in e for e in errs)


def test_validate_ignores_absent_jump():
    assert validate_editor_hosts([{"name": "c6", "ssh": "svc@c6"}]) == []


# ── TOML round-trip ───────────────────────────────────────────────────────────


def test_hosts_toml_round_trips_jump(tmp_path):
    p = tmp_path / "runspec_hosts.toml"
    save_hosts(
        p,
        [
            {
                "name": "c6",
                "ssh": "svc@c6",
                "jump": "bastion",
                "runspec_paths": ["runspec"],
            }
        ],
    )
    assert load_hosts(p)[0]["jump"] == "bastion"


# ── bridge routing (discovery + connectivity) ─────────────────────────────────


def _bridge():
    from runspec_console.bridge import Bridge

    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        return Bridge()


def test_check_connected_routes_jump_host_through_system_ssh():
    b = _bridge()
    host = {"name": "c6", "ssh": "svc@c6", "jump": "bastion"}
    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch("runspec_console.executor.ssh_jump_run", return_value=(0, "", "")) as m,
    ):
        assert b._check_connected(host) is True
    assert m.call_args.kwargs["jump"] == "bastion"


def test_check_connected_jump_host_down_reports_false():
    b = _bridge()
    host = {"name": "c6", "ssh": "svc@c6", "jump": "bastion"}
    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch(
            "runspec_console.executor.ssh_jump_run", return_value=(255, "", "no route")
        ),
    ):
        assert b._check_connected(host) is False


def test_discover_one_routes_jump_host_through_system_ssh():
    b = _bridge()
    b._connected_cache["c6"] = True
    host = {
        "name": "c6",
        "ssh": "svc@c6",
        "jump": "bastion",
        "runspec_paths": ["/opt/bin/runspec"],
    }
    calls: dict = {}

    def fake_jump_run(
        target, command, *, jump, identity_file=None, global_ssh_config=None, timeout=15
    ):
        calls["jump"] = jump
        calls["command"] = command
        return (0, "[]", "")

    with (
        patch.object(b, "get_config", return_value={"ssh": {}}),
        patch("runspec_console.executor.ssh_jump_run", side_effect=fake_jump_run),
    ):
        out = b._discover_one(host)
    assert out == []
    assert calls["jump"] == "bastion"
    assert "local --format json" in calls["command"]

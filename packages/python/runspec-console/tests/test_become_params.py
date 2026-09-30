"""run_as resolution for a discovered runnable — the "@owner" default."""

from __future__ import annotations

from runspec_console.bridge_invoke import InvokeMixin

_bp = InvokeMixin()._become_params


def _rec(run_as=None, config=None, owner="svc", login="alice"):
    return {
        "rawSpec": {"run_as": run_as},
        "configRunAs": config,
        "venvOwner": owner,
        "loginUser": login,
    }


def test_default_is_the_venv_owner():
    assert _bp(_rec(), "h")[0] == "svc"


def test_owner_is_the_login_account_means_no_escalation():
    assert _bp(_rec(owner="alice"), "h")[0] == ""


def test_root_owned_or_unknown_owner_means_no_escalation():
    assert _bp(_rec(owner=""), "h")[0] == ""
    assert _bp(_rec(owner=None), "h")[0] == ""


def test_config_run_as_then_runnable_run_as():
    assert _bp(_rec(config=""), "h")[0] == ""
    assert _bp(_rec(config="ops"), "h")[0] == "ops"
    assert _bp(_rec(run_as="root", config=""), "h")[0] == "root"
    assert _bp(_rec(run_as="", config="ops"), "h")[0] == ""


def test_per_host_table_can_name_owner():
    spec = {"default": "@owner", "hosts": {"db1": "postgres"}}
    assert _bp(_rec(run_as=spec), "db1")[0] == "postgres"
    assert _bp(_rec(run_as=spec), "web1")[0] == "svc"


def test_explicit_login_account_is_not_escalated():
    assert _bp(_rec(run_as="alice"), "h")[0] == ""


def test_record_from_an_older_discovery_never_escalates():
    assert _bp({"rawSpec": {}}, "h")[0] == ""
    assert _bp(None, "h") == ("", "sudo", None)


def test_discovery_shows_the_resolved_account():
    from runspec_console.discovery import stamp_identity

    def shown(raw_run_as=None, config=None, owner="svc", login="alice"):
        [r] = stamp_identity(
            [
                {"rawSpec": {"run_as": raw_run_as}}
                | ({"runAs": raw_run_as} if raw_run_as else {})
            ],
            config,
            owner,
            login,
        )
        return r.get("runAs")

    assert shown() == "svc"
    assert shown(owner="alice") is None
    assert shown(owner="") is None
    assert shown(config="") is None
    assert shown(config="ops") == "ops"
    assert shown(raw_run_as="root") == "root"
    assert shown(raw_run_as="@owner") == "svc"


def test_windows_local_venv_never_escalates(monkeypatch, tmp_path):
    # No POSIX uids: owner and current user are both unknown ⇒ no escalation.
    import os

    from runspec_console.discovery import local_identity, stamp_identity

    monkeypatch.delattr(os, "geteuid", raising=False)
    (tmp_path / "Scripts").mkdir()
    owner, user = local_identity(str(tmp_path / "Scripts" / "runspec.exe"))
    assert (owner, user) == (None, "")
    [r] = stamp_identity([{"rawSpec": {"run_as": None}}], None, owner, user)
    assert _bp(r, "local")[0] == "" and "runAs" not in r


def test_windows_remote_host_via_cli_fallback_never_escalates():
    # A Windows host can't run the POSIX scan; its runspec local reports "".
    from runspec_console.discovery import _item_to_runnables

    item = {
        "runnable": "t",
        "spec": {"run_as": None},
        "config_autonomy": None,
        "config_run_as": None,
        "env_owner": "",
        "user": "",
    }
    [r] = _item_to_runnables(item, "win-01", "venv")
    assert _bp(r, "win-01")[0] == ""


def test_fallback_command_is_not_posix_quoted():
    from runspec_console import discovery

    calls = []

    def runner(target, cmd, **kw):
        calls.append(cmd)
        return (1, "", "") if len(calls) == 1 else (0, "[]", "")

    discovery.discover_remote(
        "win", r"C:\venvs\ops\Scripts\runspec.exe", "win", runner=runner
    )
    assert calls[1] == r"C:\venvs\ops\Scripts\runspec.exe local --format json"


def test_scan_reports_no_owner_under_a_windows_posix_shell(tmp_path):
    # Git Bash / MSYS on a Windows host: stat/id exist, but no owner is reported.
    import shutil
    import subprocess
    import sys

    import pytest

    from runspec_console.discovery import _parse_scan_identity, _scan_command

    sh = shutil.which("sh")
    if not sh or sys.platform == "win32":  # a real Windows shell needs no fake uname
        pytest.skip("needs a POSIX sh on a POSIX host")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uname").write_text("#!/bin/sh\necho MINGW64_NT-10.0\n")
    (fake / "uname").chmod(0o755)
    (tmp_path / "bin").mkdir()
    env = {"PATH": f"{fake}:/usr/bin:/bin"}
    out = subprocess.run(
        [sh, "-c", _scan_command(str(tmp_path / "bin" / "runspec"))],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout
    assert _parse_scan_identity(out) == (None, "")

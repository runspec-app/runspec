"""
test_cred_env_exec.py — credential env vars reach the (local) child process.

``run_local`` injects ``cred_env`` (plain, name-derived credential vars like
JIRA_PASSWORD) into the runnable's environment, distinct from the RUNSPEC_*_ARG_*
secret channel. This runs a tiny fake runnable that echoes an env var and asserts
the value arrives. No escalation (no ``run_as``) so the runspec core import the
executor does for sudo isn't needed.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from runspec_console.executor import run_local


def _make_fake_runnable(tmp_path: Path, name: str, body: str) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / name
    script.write_text("#!/usr/bin/env bash\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    # run_local derives bin_dir from the runspec path's parent.
    return str(bin_dir / "runspec")


def test_cred_env_reaches_child(tmp_path):
    rp = _make_fake_runnable(
        tmp_path, "echoenv", 'echo "P=$JIRA_PASSWORD U=$JIRA_USERNAME"'
    )
    lines: list[str] = []
    done: list[int] = []

    run_local(
        rp,
        "echoenv",
        {},
        [],
        on_line=lambda line, stream: lines.append(line),
        on_done=lambda code, ms: done.append(code),
        cred_env={"JIRA_USERNAME": "alice", "JIRA_PASSWORD": "s3cret"},
    )

    assert done == [0]
    assert any("P=s3cret U=alice" in line for line in lines), lines


def test_no_cred_env_is_harmless(tmp_path):
    rp = _make_fake_runnable(tmp_path, "echoenv", 'echo "P=[$JIRA_PASSWORD]"')
    lines: list[str] = []
    run_local(
        rp,
        "echoenv",
        {},
        [],
        on_line=lambda line, stream: lines.append(line),
        on_done=lambda code, ms: None,
    )
    assert any("P=[]" in line for line in lines), lines


def test_cred_env_not_in_parent(tmp_path):
    # The vars are scoped to the child, not leaked into the console's own env.
    rp = _make_fake_runnable(tmp_path, "echoenv", "true")
    run_local(
        rp,
        "echoenv",
        {},
        [],
        on_line=lambda line, stream: None,
        on_done=lambda code, ms: None,
        cred_env={"JIRA_PASSWORD": "s3cret"},
    )
    assert "JIRA_PASSWORD" not in os.environ

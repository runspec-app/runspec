"""
test_secret_argv.py — escalated runs never expose env values on a command line.

A process's argument vector is readable by every user on the host (``ps``,
``/proc/<pid>/cmdline``) and ``sudo`` logs it, so the SPEC "Command building"
table passes env values on stdin (the env frame) instead. These tests run a real
escalation (root → ``nobody`` via ``sudo`` and ``su``) and look at the process
table *as an unrelated account* while the run is live.

Root only — skipped otherwise; CI's ``python-multiuser`` job runs them with
``RUNSPEC_MULTIUSER_REQUIRED=1`` so they can never pass by skipping.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
import uuid
import warnings

import pytest

from runspec.become import build_become_argv, build_exec_argv

TARGET = "nobody"
OBSERVER = 4299  # an unrelated uid (no account needed to run ps)
# Random per run, so nothing else on the host (a shell history line, this test's
# own source in someone's editor) can contain it and fake a leak.
TOKEN = uuid.uuid4().hex
SECRET = f"s3cr3t-{TOKEN} q'\"$x `id` \\n ünï\nsecond line"
NAME = "RUNSPEC_DEMO_ARG_PASSWORD"
MARKER = "rs-secret-probe"  # in the probe's argv, so we know we looked while it ran


def _skip_or_fail(why: str) -> None:
    if os.environ.get("RUNSPEC_MULTIUSER_REQUIRED"):
        pytest.fail(why)  # the CI multi-user job must never pass by skipping
    pytest.skip(why)


@pytest.fixture
def probe() -> list[str]:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        _skip_or_fail("needs root")
    python = next(
        (p for p in (shutil.which("python3"), "/usr/bin/python3") if p and subprocess.run([p, "-c", ""], preexec_fn=_demote(65534), capture_output=True).returncode == 0),
        None,
    )
    if python is None or shutil.which("sudo") is None or shutil.which("ps") is None:
        _skip_or_fail("needs sudo, ps and a python3 that nobody can run")
    code = f"import hashlib, os, sys, time; print(hashlib.sha256(os.environ[{NAME!r}].encode()).hexdigest(), flush=True); time.sleep(3)"
    return [python, "-c", code, MARKER]


def _demote(uid: int):  # type: ignore[no-untyped-def]
    def fn() -> None:
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)

    return fn


def _process_table_as_observer() -> str:
    res = subprocess.run(["ps", "-ww", "-eo", "args"], preexec_fn=_demote(OBSERVER), capture_output=True, text=True, check=True)
    return res.stdout


def _run(argv: list[str], frame: bytes | None, *, linger: float = 0.0) -> tuple[str, str]:
    """Start ``argv`` (as root), feed ``frame``, snapshot the process table as the
    observer while it runs (``linger`` seconds after it has its env), and return
    ``(table, first stdout line)``."""
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/") as proc:
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        if frame:
            proc.stdin.write(frame)
        proc.stdin.close()
        first = proc.stdout.readline().decode().strip()  # printed once the env is in place
        if linger:
            time.sleep(linger)
        table = _process_table_as_observer()
        proc.stdout.read()
        err = proc.stderr.read()
        assert proc.wait(timeout=30) == 0, err.decode()
    assert MARKER in table, "the probe wasn't running when the process table was read"
    return table, first


EXPECTED = hashlib.sha256(SECRET.encode()).hexdigest()


def test_sudo_run_keeps_the_secret_off_every_command_line(probe):
    argv, frame = build_exec_argv(probe, run_as=TARGET, become_flags="-n", env={NAME: SECRET})
    table, got = _run(argv, frame)
    assert got == EXPECTED  # the runnable received the exact value
    assert TOKEN not in table
    # sudo logs its own argv: the sudo process line is what lands in the log.
    sudo_lines = [ln for ln in table.splitlines() if ln.startswith("sudo ")]
    assert sudo_lines and not any(TOKEN in ln for ln in sudo_lines)


def test_su_run_keeps_the_secret_off_every_command_line(probe):
    if shutil.which("su") is None:
        _skip_or_fail("needs su")
    # nobody's login shell is nologin, so su needs an explicit shell.
    argv, frame = build_exec_argv(probe, run_as=TARGET, become_method="su", become_flags="-s /bin/sh", env={NAME: SECRET})
    table, got = _run(argv, frame)
    assert got == EXPECTED
    assert TOKEN not in table


def test_the_old_env_form_does_leak(probe):
    # Control: proves the observer really can see a leak, so the tests above
    # would fail if the secret reached a command line.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        argv = build_become_argv(probe, run_as=TARGET, become_flags="-n", env={NAME: SECRET})
    table, got = _run(argv, None)
    assert got == EXPECTED
    assert TOKEN in table


def test_the_secret_stays_hidden_for_the_whole_run(probe):
    # The sudo parent stays alive for the whole run; look again later in the run.
    argv, frame = build_exec_argv(probe, run_as=TARGET, become_flags="-n", env={NAME: SECRET})
    table, got = _run(argv, frame, linger=1.5)
    assert got == EXPECTED
    assert TOKEN not in table


def test_runspec_exec_path_feeds_the_frame_and_passes_stdin_through(probe, monkeypatch):
    # runspec exec / serve build the command with runspec.execute.build_command;
    # runexec._feed_stdin writes the frame and then copies its own stdin through.
    import io
    import sys

    from runspec import runexec
    from runspec.execute import build_command

    spec = {"command": probe[:3], "run_as": TARGET, "become_flags": "-n"}
    cmd, _extra, frame = build_command(spec, [MARKER], {NAME: SECRET})
    assert frame is not None and not any(TOKEN in c for c in cmd)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"piped input\n")))
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/") as proc:
        runexec._feed_stdin(proc, frame)
        assert proc.stdout is not None and proc.stderr is not None
        got = proc.stdout.readline().decode().strip()
        table = _process_table_as_observer()
        proc.stdout.read()
        proc.stderr.read()
        assert proc.wait(timeout=30) == 0
    assert got == EXPECTED
    assert MARKER in table and TOKEN not in table

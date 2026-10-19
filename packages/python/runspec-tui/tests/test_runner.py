from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from runspec import runexec, runsdb

from runspec_tui.runner import LABEL, Run


def _collect() -> tuple[list[tuple[str, str]], threading.Event, list[int]]:
    return [], threading.Event(), []


def _start(name: str, argv: list[str], env: dict[str, str], **kw):
    lines, done, codes = _collect()

    def on_done(code: int, _secs: float) -> None:
        codes.append(code)
        done.set()

    run = Run(name, argv, env, on_line=lambda s, ln: lines.append((s, ln)), on_done=on_done, **kw)
    run.start()
    return run, lines, done, codes


def test_runs_like_runspec_exec_with_stdin_closed() -> None:
    seen: dict = {}

    def fake(name, argv, **kw):
        seen.update(kw, name=name, argv=argv)
        kw["on_start"](subprocess.Popen([sys.executable, "-c", "pass"]))
        kw["stdout"].buffer.write(b"one\ntwo\npartial")
        kw["stderr"].write("warn\n")
        return 3

    _run, lines, done, codes = _start("demo", ["--x", "1"], {"RUNSPEC_DEMO_ARG_TOKEN": "t"}, executor=fake)
    assert done.wait(5)
    assert codes == [3]
    assert seen["name"] == "demo" and seen["argv"] == ["--x", "1"]
    assert seen["label"] == LABEL
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["env"] == {"RUNSPEC_DEMO_ARG_TOKEN": "t", "PYTHONUNBUFFERED": "1"}
    assert lines == [("stdout", "one"), ("stdout", "two"), ("stderr", "warn"), ("stdout", "partial")]


def test_an_executor_failure_is_reported_not_raised() -> None:
    def boom(name, argv, **kw):
        raise RuntimeError("no such venv")

    _run, lines, done, codes = _start("demo", [], {}, executor=boom)
    assert done.wait(5)
    assert codes == [1]
    assert lines == [("stderr", "runspec-tui: could not run demo: no such venv")]


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real `runspec exec` of a small script, recorded in a tmp logs dir."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(runexec, "_resolve_log_dir", lambda: logs)

    def install(body: str) -> Path:
        script = tmp_path / "demo.py"
        script.write_text(textwrap.dedent(body))
        spec = {"runnable": "demo", "command": [sys.executable, str(script)], "config_path": "", "run_as": "", "become_method": "sudo", "become_flags": None, "config": {}}
        monkeypatch.setattr(runexec, "find_runnable", lambda name: spec if name == "demo" else None)
        return logs

    return install


def test_real_run_streams_output_and_is_recorded(installed) -> None:
    logs = installed(
        """
        import os, sys
        print("args", sys.argv[1:])
        print("token", os.environ.get("RUNSPEC_DEMO_ARG_TOKEN"))
        print("stdin", repr(sys.stdin.read()))
        print("bad", file=sys.stderr)
        sys.exit(2)
        """
    )
    _run, lines, done, codes = _start("demo", ["--x", "1"], {"RUNSPEC_DEMO_ARG_TOKEN": "t"})
    assert done.wait(20)
    assert codes == [2]
    assert ("stdout", "args ['--x', '1']") in lines
    assert ("stdout", "token t") in lines
    assert ("stdout", "stdin ''") in lines
    assert ("stderr", "bad") in lines
    [row] = runsdb.list_runs(logs, live=False, limit=5)
    assert row["exit_code"] == 2 and row["label"] == LABEL


def test_stop_ends_the_run_and_it_is_still_recorded(installed) -> None:
    logs = installed(
        """
        import time
        print("started", flush=True)
        time.sleep(30)
        """
    )
    run, lines, done, codes = _start("demo", [], {})
    for _ in range(100):
        if lines:
            break
        threading.Event().wait(0.1)
    run.cancel()
    assert done.wait(20)
    assert run.cancelled and codes and codes[0] != 0
    [row] = runsdb.list_runs(logs, live=False, limit=5)
    assert row["status"] == "failed"

"""
test_exec.py — `runspec exec` (runspec/runexec.py): one recorded row per run with
the observed exit code, captured output in the run's log, skips, timeouts, and
the RUNSPEC_RUN_ID handoff to the child. Children are small scripts; the logs
dir and the runnable resolution are pointed at tmp paths.
"""

from __future__ import annotations

import io
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from runspec import runexec, runsdb
from runspec.logging_setup import adopt_exec_run_id


@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setattr(runexec, "_resolve_log_dir", lambda: d)
    monkeypatch.delenv("RUNSPEC_TRIGGER", raising=False)
    monkeypatch.delenv("RUNSPEC_LABEL", raising=False)
    return d


def _install(monkeypatch, tmp_path, body: str, *, config: dict | None = None) -> None:
    """Make `runspec exec demo` run a python script with ``body``."""
    script = tmp_path / "demo.py"
    script.write_text(textwrap.dedent(body))
    spec = {
        "runnable": "demo",
        "command": [sys.executable, str(script)],
        "config_path": "",
        "run_as": "",
        "become_method": "sudo",
        "become_flags": None,
        "config": config or {},
    }
    monkeypatch.setattr(runexec, "find_runnable", lambda name: spec if name == "demo" else None)


def _run(**kw) -> tuple[int, str, str]:
    out, err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8"), io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    rc = runexec.exec_runnable(kw.pop("name", "demo"), kw.pop("argv", []), stdout=out, stderr=err, **kw)
    out.flush()
    err.flush()
    return rc, out.buffer.getvalue().decode(), err.buffer.getvalue().decode()  # type: ignore[attr-defined]


def _rows(logs_dir: Path) -> list[dict]:
    return runsdb.list_runs(logs_dir, live=False, limit=50)


def _records(logs_dir: Path, row: dict) -> list[dict]:
    lines = (logs_dir / row["log_file"]).read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class TestPlainChild:
    def test_records_observed_exit_code_and_output(self, logs_dir, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path, "import sys\nprint('hello out')\nprint('oops', file=sys.stderr)\nsys.exit(2)\n")
        rc, out, err = _run(label="sched:x", trigger="schedule")
        assert rc == 2
        assert "hello out" in out and "oops" in err  # tee'd through
        [row] = _rows(logs_dir)
        assert row["status"] == "failed" and row["exit_code"] == 2
        assert row["label"] == "sched:x" and row["trigger"] == "schedule"
        recs = _records(logs_dir, row)
        outputs = {r["extra"]["stream"]: r["extra"] for r in recs if r["extra"].get("event") == "exec_output"}
        assert outputs["stdout"]["text"] == "hello out\n"
        assert outputs["stderr"]["text"] == "oops\n"
        [result] = [r for r in recs if r["extra"].get("event") == "exec_result"]
        assert result["extra"]["exit_code"] == 2
        assert all(r["extra"]["run_id"] == row["run_id"] for r in recs)
        # the per-run filename grammar, so `runspec logs` manages it
        from runspec.logs import classify_name

        assert classify_name(row["log_file"]) == "per_run"

    def test_success(self, logs_dir, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path, "print('ok')\n")
        rc, _, _ = _run()
        assert rc == 0
        [row] = _rows(logs_dir)
        assert row["status"] == "completed" and row["exit_code"] == 0

    def test_capture_keeps_the_tail(self, logs_dir, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path, "import sys\nsys.stdout.write('a' * 5000 + 'END')\n")
        _run(capture_bytes=100)
        [row] = _rows(logs_dir)
        [o] = [r["extra"] for r in _records(logs_dir, row) if r["extra"].get("event") == "exec_output"]
        assert o["truncated"] is True and o["total_bytes"] == 5003
        assert len(o["text"]) == 100 and o["text"].endswith("END")

    def test_capture_zero_disables_capture(self, logs_dir, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path, "print('x')\n")
        rc, _, _ = _run(capture_bytes=0)
        assert rc == 0
        [row] = _rows(logs_dir)
        assert not [r for r in _records(logs_dir, row) if r["extra"].get("event") == "exec_output"]

    def test_timeout(self, logs_dir, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path, "import time\ntime.sleep(30)\n")
        rc, _, _ = _run(timeout=1)
        assert rc == runexec.EXIT_TIMEOUT
        [row] = _rows(logs_dir)
        assert row["status"] == "failed" and row["exit_code"] == 124

    def test_db_false_writes_no_row_but_keeps_output(self, logs_dir, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path, "print('x')\n", config={"history": {"db": False}})
        assert _run()[0] == 0
        assert _rows(logs_dir) == []
        assert list(logs_dir.glob("demo.*.log"))


class TestLabel:
    """The run's label: --label, else the launcher's RUNSPEC_LABEL, else "cli" —
    recorded on the row and handed to the child as RUNSPEC_LABEL."""

    _BODY = "import os\nprint(os.environ.get('RUNSPEC_LABEL', '<unset>'))\n"

    def test_unlabelled_run_is_cli(self, logs_dir, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path, self._BODY)
        _, out, _ = _run()
        [row] = _rows(logs_dir)
        assert row["label"] == "cli" and out.strip() == "cli"

    def test_launcher_label_from_env(self, logs_dir, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path, self._BODY)
        monkeypatch.setenv("RUNSPEC_LABEL", "console")
        _, out, _ = _run()
        [row] = _rows(logs_dir)
        assert row["label"] == "console" and out.strip() == "console"

    def test_explicit_label_wins(self, logs_dir, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path, self._BODY)
        monkeypatch.setenv("RUNSPEC_LABEL", "console")
        _, out, _ = _run(label="tui")
        [row] = _rows(logs_dir)
        assert row["label"] == "tui" and out.strip() == "tui"

    def test_unlabelled_runs_do_not_skip_each_other(self, logs_dir, tmp_path, monkeypatch):
        # --skip-if-running keys on --label only, so the "cli" default never
        # makes one unlabelled runnable wait on another.
        _install(monkeypatch, tmp_path, "print('ran')\n")
        runsdb.begin_run(
            logs_dir,
            {"run_id": "live1", "runnable": "other", "status": "running", "pid": os.getpid(), "host": runsdb._hostname(), "proc_start": runsdb.proc_start_token(), "label": "cli"},
        )
        _, out, _ = _run(skip_if_running=True)
        assert "ran" in out


class TestNotRunnable:
    def test_not_installed_is_recorded(self, logs_dir, monkeypatch):
        monkeypatch.setattr(runexec, "find_runnable", lambda name: None)
        rc, _, err = _run(name="ghost", label="sched:ghost")
        assert rc == runexec.EXIT_NOT_FOUND
        assert "not installed" in err
        [row] = _rows(logs_dir)
        assert row["runnable"] == "ghost" and row["status"] == "failed" and row["label"] == "sched:ghost"


class TestSkips:
    def test_lock_held_records_skipped(self, logs_dir, tmp_path, monkeypatch):
        pytest.importorskip("fcntl")
        _install(monkeypatch, tmp_path, "print('ran')\n")
        lock = str(tmp_path / "l.lock")
        held = runexec._try_lock(lock)
        try:
            rc, out, err = _run(lock=lock, label="sched:x")
        finally:
            held.close()
        assert rc == 0 and "ran" not in out and "skipped" in err
        [row] = _rows(logs_dir)
        assert row["status"] == "skipped" and row["label"] == "sched:x"

    def test_lock_free_runs(self, logs_dir, tmp_path, monkeypatch):
        pytest.importorskip("fcntl")
        _install(monkeypatch, tmp_path, "print('ran')\n")
        rc, out, _ = _run(lock=str(tmp_path / "l.lock"))
        assert rc == 0 and "ran" in out

    def test_skip_if_running_by_label(self, logs_dir, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path, "print('ran')\n")
        # a live running row with the same label (our own pid → alive)
        runsdb.begin_run(
            logs_dir,
            {"run_id": "live1", "runnable": "demo", "status": "running", "pid": os.getpid(), "host": runsdb._hostname(), "proc_start": runsdb.proc_start_token(), "label": "sched:x"},
        )
        rc, out, _ = _run(skip_if_running=True, label="sched:x")
        assert rc == 0 and "ran" not in out
        assert {r["status"] for r in _rows(logs_dir)} == {"running", "skipped"}
        # a different label is not blocked
        rc, out, _ = _run(skip_if_running=True, label="sched:y")
        assert "ran" in out


class TestHandoff:
    def test_child_writing_the_handed_down_id_is_one_row(self, logs_dir, tmp_path, monkeypatch):
        # What a >= 0.62 child does: adopt RUNSPEC_RUN_ID and upsert its own row
        # (its pid, a label-less row), recording its *inferred* exit code 0.
        body = f"""
        import os, sys
        sys.path.insert(0, {str(Path(runsdb.__file__).parent.parent)!r})
        from pathlib import Path
        from runspec import runsdb
        from runspec.logging_setup import adopt_exec_run_id
        rid = adopt_exec_run_id()
        assert rid and "RUNSPEC_RUN_ID" not in os.environ
        d = Path({str(logs_dir)!r})
        runsdb.begin_run(d, {{"run_id": rid, "runnable": "demo", "command": "sub", "status": "running",
                              "pid": os.getpid(), "host": runsdb._hostname(), "args": {{"a": 1}}}})
        runsdb.finish_run(d, rid, exit_code=0, duration_ms=1, status="completed", events={{"INFO": 2}})
        sys.exit(3)
        """
        _install(monkeypatch, tmp_path, body)
        rc, _, _ = _run(label="sched:x")
        assert rc == 3
        [row] = _rows(logs_dir)
        assert row["exit_code"] == 3 and row["status"] == "failed"  # observed, not inferred
        assert row["label"] == "sched:x"  # the child's upsert didn't clear it
        assert row["command"] == "sub"  # the child's own detail kept
        assert row["events"] == '{"INFO": 2}'  # the wrapper's finalize kept the child's events

    def test_child_label_does_not_replace_the_wrappers(self, logs_dir, tmp_path, monkeypatch):
        # A child that records its own label (an older wrapper never handed one
        # down, so parse() fell back to "cli") must not overwrite --label.
        body = f"""
        import os, sys
        sys.path.insert(0, {str(Path(runsdb.__file__).parent.parent)!r})
        from pathlib import Path
        from runspec import runsdb
        from runspec.logging_setup import adopt_exec_run_id
        rid = adopt_exec_run_id()
        d = Path({str(logs_dir)!r})
        runsdb.begin_run(d, {{"run_id": rid, "runnable": "demo", "status": "running", "label": "cli",
                              "pid": os.getpid(), "host": runsdb._hostname()}})
        runsdb.finish_run(d, rid, exit_code=0, duration_ms=1, status="completed")
        """
        _install(monkeypatch, tmp_path, body)
        _run(label="sched:x")
        [row] = _rows(logs_dir)
        assert row["label"] == "sched:x"

    def test_older_child_row_is_adopted(self, logs_dir, tmp_path, monkeypatch):
        # What a <= 0.61 child does: ignore RUNSPEC_RUN_ID and mint its own id.
        body = f"""
        import os, sys, uuid
        sys.path.insert(0, {str(Path(runsdb.__file__).parent.parent)!r})
        from pathlib import Path
        from runspec import runsdb
        d = Path({str(logs_dir)!r})
        rid = str(uuid.uuid4())
        runsdb.begin_run(d, {{"run_id": rid, "runnable": "demo", "command": "old", "status": "running",
                              "pid": os.getpid(), "host": runsdb._hostname(), "start_ts": runsdb._now_iso()}})
        runsdb.finish_run(d, rid, exit_code=0, duration_ms=1, status="completed")
        """
        _install(monkeypatch, tmp_path, body)
        rc, _, _ = _run(label="sched:x")
        assert rc == 0
        [row] = _rows(logs_dir)
        assert row["command"] == "old" and row["label"] == "sched:x"


class TestAdoptRunId:
    def test_valid_id_adopted_and_removed(self, monkeypatch):
        monkeypatch.setenv("RUNSPEC_RUN_ID", "0f1e2d3c-aaaa-bbbb-cccc-1234567890ab")
        assert adopt_exec_run_id() == "0f1e2d3c-aaaa-bbbb-cccc-1234567890ab"
        assert "RUNSPEC_RUN_ID" not in os.environ

    def test_malformed_id_ignored_and_removed(self, monkeypatch):
        monkeypatch.setenv("RUNSPEC_RUN_ID", "../../etc/passwd")
        assert adopt_exec_run_id() is None
        assert "RUNSPEC_RUN_ID" not in os.environ

    def test_parse_removes_it_even_without_logging(self, tmp_path, monkeypatch):
        from runspec import parse

        cfg = tmp_path / "runspec.toml"
        cfg.write_text('[demo]\ndescription = "d"\n')
        monkeypatch.setenv("RUNSPEC_RUN_ID", "0f1e2d3c-aaaa-bbbb-cccc-1234567890ab")
        parse("demo", argv=[], config_path=cfg)
        assert "RUNSPEC_RUN_ID" not in os.environ


def test_cli_exec_exits_with_child_code(logs_dir, tmp_path, monkeypatch):
    from runspec import cli

    _install(monkeypatch, tmp_path, "import sys\nsys.exit(5)\n")
    with pytest.raises(SystemExit) as e:
        cli.cmd_exec(["--label", "t", "demo", "--", "--x", "1"])
    assert e.value.code == 5
    [row] = _rows(logs_dir)
    assert row["label"] == "t"


def test_env_reaches_child_and_self_run_as_skips_become(logs_dir, tmp_path, monkeypatch):
    script = tmp_path / "demo.py"
    script.write_text("import os\nprint(os.environ.get('RUNSPEC_ENV_FILE', ''))\n")
    spec = {"runnable": "demo", "command": [sys.executable, str(script)], "config_path": "", "run_as": "svc", "become_method": "sudo", "become_flags": None, "config": {}}
    monkeypatch.setattr(runexec, "find_runnable", lambda name: spec)
    monkeypatch.setattr(runexec, "_effective_user", lambda: "svc")  # already the run_as account
    rc, out, _ = _run(env={"RUNSPEC_ENV_FILE": "/x/y.env"})
    assert rc == 0 and out.strip() == "/x/y.env"  # ran directly (no sudo) with the env


class TestInProcessCaller:
    """The hooks a terminal UI calling exec_runnable in-process relies on."""

    def test_stdin_devnull_keeps_child_off_the_terminal(self, logs_dir, tmp_path, monkeypatch):
        import subprocess

        _install(monkeypatch, tmp_path, "import sys\nprint(repr(sys.stdin.read()))\n")
        rc, out, _ = _run(stdin=subprocess.DEVNULL)
        assert rc == 0
        assert out.strip() == "''"

    def test_on_start_receives_the_child_and_can_stop_it(self, logs_dir, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path, "import time\ntime.sleep(30)\n")
        seen = []

        def stop(proc):
            seen.append(proc.pid)
            proc.terminate()

        rc, _, _ = _run(on_start=stop)
        assert seen and rc != 0
        [row] = _rows(logs_dir)
        assert row["status"] == "failed" and row["exit_code"] == rc

    def test_a_failing_on_start_does_not_orphan_the_run(self, logs_dir, tmp_path, monkeypatch):
        _install(monkeypatch, tmp_path, "print('ran')\n")

        def broken(proc):
            raise RuntimeError("caller bug")

        rc, out, _ = _run(on_start=broken)
        assert rc == 0 and "ran" in out
        [row] = _rows(logs_dir)
        assert row["status"] == "completed"

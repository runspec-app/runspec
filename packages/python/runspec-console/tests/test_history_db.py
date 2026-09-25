"""
test_history_db.py — the DB-backed History read (Phase 3 part 2a).

Covers the runs-index row → HistoryRecord mapper (shape parity with _parse_log)
and the slim History read swapping to the per-venv runspec_runs.db with a
log-parse fallback where a venv has no (or an empty) DB.
"""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from runspec import runsdb

from runspec_console import bridge_utils
from runspec_console.bridge import Bridge


def _make_bridge():
    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        b = Bridge()
    b._ssh_pool = None
    return b


def _db_row(**over):
    base = {
        "run_id": "run-abc",
        "runnable": "backup",
        "command": "",
        "status": "completed",
        "exit_code": 0,
        "start_ts": "2026-09-25T09:59:59+00:00",
        "end_ts": "2026-09-25T10:00:00+00:00",
        "duration_ms": 1234,
        "user": "alice",
        "user_target": "svc",
        "run_as": "",
        "agent": 0,
        "trigger": "",
        "skill": "",
        "autonomy": "confirm",
        "pid": 4242,
        "host": "prod-1",
        "proc_start": "",
        "log_file": "backup.x.run-abc.log",
        "args": '{"target": "prod"}',
    }
    base.update(over)
    return base


class TestDbRowToRecord(unittest.TestCase):
    def test_maps_fields_and_id_hash(self) -> None:
        r = bridge_utils.db_row_to_record(_db_row(), "ops-tools", "prod-1")
        # id is the same md5(run_id+runnable)[:12] a log parse produces, so a DB
        # row and a parsed row for the same run collapse to one History row.
        self.assertEqual(r["id"], hashlib.md5(b"run-abcbackup").hexdigest()[:12])
        self.assertEqual(r["runId"], "run-abc")
        self.assertEqual(r["runnable"], "backup")
        self.assertEqual(r["group"], "ops-tools")
        self.assertEqual(r["host"], "prod-1")
        self.assertEqual(r["operator"], "alice")
        self.assertEqual(r["runAs"], "svc")
        self.assertEqual(r["exitCode"], 0)
        self.assertEqual(r["durationMs"], 1234)
        self.assertEqual(r["ts"], "2026-09-25T10:00:00+00:00")  # end_ts, the summary ts
        self.assertEqual(r["args"], {"target": "prod"})
        self.assertEqual(r["initiatedBy"], "user")
        self.assertEqual(r["kind"], "runnable")
        self.assertEqual(r["logLines"], [])  # heavy lines never in the index

    def test_command_splits_to_path_and_agent_and_runbook(self) -> None:
        r = bridge_utils.db_row_to_record(
            _db_row(command="db migrate", agent=1, skill="nightly", trigger="schedule"),
            "core",
            "local",
        )
        self.assertEqual(r["commandPath"], ["db", "migrate"])
        self.assertEqual(r["initiatedBy"], "llm")  # agent flag
        self.assertEqual(r["runbook"], "nightly")  # index `skill` column
        self.assertEqual(r["trigger"], "schedule")

    def test_crashed_row_null_exit_reads_as_failed(self) -> None:
        r = bridge_utils.db_row_to_record(
            _db_row(status="crashed", exit_code=None, end_ts=None), "core", "local"
        )
        self.assertEqual(r["exitCode"], 1)  # never a false success
        self.assertEqual(r["ts"], "2026-09-25T09:59:59+00:00")  # falls back to start_ts

    def test_bad_args_json_is_empty(self) -> None:
        r = bridge_utils.db_row_to_record(_db_row(args="not json"), "core", "local")
        self.assertEqual(r["args"], {})


def _venv_with_db(tmp: Path, name: str) -> tuple[str, Path]:
    """A venv tree ({venv}/bin/runspec + {venv}/logs) — returns (rp, logs_dir)."""
    venv = tmp / name
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "runspec").write_text("#!/bin/sh\n")
    logs = venv / "logs"
    logs.mkdir()
    return str(venv / "bin" / "runspec"), logs


def _write_db_run(logs: Path, run_id: str, runnable: str) -> None:
    runsdb.begin_run(
        logs,
        {
            "run_id": run_id,
            "runnable": runnable,
            "status": "running",
            "start_ts": "2026-09-25T10:00:00+00:00",
            "user": "alice",
            "agent": False,
            "pid": os.getpid(),
            "host": runsdb._hostname(),
            "proc_start": runsdb.proc_start_token(),
            "log_file": f"{runnable}.x.{run_id}.log",
            "args": {"t": "prod"},
        },
    )
    runsdb.finish_run(logs, run_id, exit_code=0, duration_ms=42, status="completed")


def _write_log_run(logs: Path, run_id: str, runnable: str) -> None:
    line = {
        "ts": "2026-09-25T11:00:00+00:00",
        "level": "INFO",
        "message": f"{runnable} done",
        "extra": {
            "event": "run_summary",
            "run_id": run_id,
            "runnable": runnable,
            "user": "bob",
            "exit_code": 0,
            "duration_ms": 5,
        },
    }
    (logs / f"{runnable}.20260925T110000Z.{run_id}.log").write_text(
        json.dumps(line) + "\n"
    )


class TestSlimHistoryDbLocal(unittest.TestCase):
    def _bridge_for(self, tmp: Path, rps: list[str]):
        b = _make_bridge()
        b._hosts = [{"name": "ws", "runspec_paths": rps}]
        return b

    def test_reads_from_db(self) -> None:
        import tempfile

        # ignore_cleanup_errors: on Windows SQLite (WAL) briefly holds the .db file
        # after close, so rmtree of the temp dir races with WinError 32. The product
        # never rmtrees a live logs dir; this is a test-teardown artifact only.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            tmp = Path(d)
            rp, logs = _venv_with_db(tmp, "v1")
            _write_db_run(logs, "run-1", "backup")
            b = self._bridge_for(tmp, [rp])
            home = tmp / "home"
            home.mkdir()
            with patch.object(Path, "home", return_value=home):
                rows = b.get_history_slim("ws", since_days=None)
            ids = {r["runId"] for r in rows}
            self.assertIn("run-1", ids)
            row = next(r for r in rows if r["runId"] == "run-1")
            self.assertEqual(row["runnable"], "backup")
            self.assertEqual(row["group"], "v1")
            self.assertEqual(row["exitCode"], 0)
            self.assertEqual(row["logLines"], [])  # slim — index carries no lines

    def test_falls_back_to_log_parse_when_no_db(self) -> None:
        import tempfile

        # ignore_cleanup_errors: on Windows SQLite (WAL) briefly holds the .db file
        # after close, so rmtree of the temp dir races with WinError 32. The product
        # never rmtrees a live logs dir; this is a test-teardown artifact only.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            tmp = Path(d)
            rp, logs = _venv_with_db(tmp, "v1")  # logs dir but no DB
            _write_log_run(logs, "run-2", "deploy")
            b = self._bridge_for(tmp, [rp])
            home = tmp / "home"
            home.mkdir()
            with patch.object(Path, "home", return_value=home):
                rows = b.get_history_slim("ws", since_days=None)
            self.assertIn("run-2", {r["runId"] for r in rows})

    def test_unions_db_venv_and_logparse_venv(self) -> None:
        import tempfile

        # ignore_cleanup_errors: on Windows SQLite (WAL) briefly holds the .db file
        # after close, so rmtree of the temp dir races with WinError 32. The product
        # never rmtrees a live logs dir; this is a test-teardown artifact only.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            tmp = Path(d)
            rp1, logs1 = _venv_with_db(tmp, "v1")
            _write_db_run(logs1, "run-db", "backup")  # v1 has a DB
            rp2, logs2 = _venv_with_db(tmp, "v2")
            _write_log_run(logs2, "run-log", "deploy")  # v2 log-only
            b = self._bridge_for(tmp, [rp1, rp2])
            home = tmp / "home"
            home.mkdir()
            with patch.object(Path, "home", return_value=home):
                rows = b.get_history_slim("ws", since_days=None)
            ids = {r["runId"] for r in rows}
            self.assertIn("run-db", ids)  # from the DB venv
            self.assertIn("run-log", ids)  # from the log-parsed venv — never hidden


def _framed(runnable: str, run_id: str) -> str:
    line = json.dumps(
        {
            "ts": "2026-09-25T11:00:00+00:00",
            "level": "INFO",
            "message": "done",
            "extra": {
                "event": "run_summary",
                "run_id": run_id,
                "runnable": runnable,
                "user": "bob",
                "exit_code": 0,
                "duration_ms": 5,
            },
        }
    )
    return f"\x00RUNSPEC_LOG:{runnable}.x.{run_id}\n{line}\n"


class TestSlimHistoryDbRemote(unittest.TestCase):
    def _bridge(self):
        b = _make_bridge()
        b._hosts = [
            {
                "name": "prod-1",
                "ssh": "u@h1",
                "runspec_paths": ["/opt/venvs/v1/bin/runspec"],
            }
        ]
        return b

    def test_reads_db_over_ssh(self) -> None:
        b = self._bridge()
        db_result = {
            "/opt/venvs/v1/logs": {
                "has_db": True,
                "rows": [_db_row(run_id="rdb", runnable="backup")],
            },
            "~/logs": {"has_db": False, "rows": []},
        }

        def ssh(_t, cmd, **kw):
            if "runspec_runs.db" in cmd:  # the DB read snippet
                return (0, json.dumps(db_result), "")
            return (0, "", "")  # ~/logs fallback stream — empty

        with patch.object(b, "_ssh_run", side_effect=ssh):
            rows = b.get_history_slim("prod-1", since_days=None)
        self.assertIn("rdb", {r["runId"] for r in rows})
        self.assertEqual(next(r for r in rows if r["runId"] == "rdb")["group"], "v1")

    def test_falls_back_to_full_log_parse_when_no_db(self) -> None:
        b = self._bridge()
        no_db = {
            "/opt/venvs/v1/logs": {"has_db": False, "rows": []},
            "~/logs": {"has_db": False, "rows": []},
        }

        def ssh(_t, cmd, **kw):
            if "runspec_runs.db" in cmd:
                return (0, json.dumps(no_db), "")  # nothing indexed → return None
            if "RUNSPEC_LOG" in cmd:  # the full remote log-parse fallback
                return (0, _framed("deploy", "rlog"), "")
            return (0, "", "")

        with patch.object(b, "_ssh_run", side_effect=ssh):
            rows = b.get_history_slim("prod-1", since_days=None)
        self.assertIn("rlog", {r["runId"] for r in rows})  # via the log-parse fallback

    def test_unions_db_and_inline_fallback(self) -> None:
        b = self._bridge()
        db_result = {
            "/opt/venvs/v1/logs": {
                "has_db": True,
                "rows": [_db_row(run_id="rdb", runnable="backup")],
            },
            "~/logs": {
                "has_db": False,
                "rows": [],
            },  # no DB → log-parse this dir inline
        }

        def ssh(_t, cmd, **kw):
            if "runspec_runs.db" in cmd:
                return (0, json.dumps(db_result), "")
            if "RUNSPEC_LOG" in cmd:  # inline fallback stream for ~/logs
                return (0, _framed("deploy", "rlog"), "")
            return (0, "", "")

        with patch.object(b, "_ssh_run", side_effect=ssh):
            rows = b.get_history_slim("prod-1", since_days=None)
        ids = {r["runId"] for r in rows}
        self.assertIn("rdb", ids)  # indexed venv
        self.assertIn("rlog", ids)  # ~/logs, log-parsed inline — never hidden

    def test_snippet_failure_returns_none_for_fallback(self) -> None:
        b = self._bridge()

        def ssh(_t, cmd, **kw):
            if "runspec_runs.db" in cmd:
                return (
                    0,
                    "not json",
                    "",
                )  # snippet output unparseable → None → fallback
            if "RUNSPEC_LOG" in cmd:
                return (0, _framed("deploy", "rlog"), "")
            return (0, "", "")

        with patch.object(b, "_ssh_run", side_effect=ssh):
            rows = b.get_history_slim("prod-1", since_days=None)
        self.assertIn("rlog", {r["runId"] for r in rows})


class TestDbReadCmd(unittest.TestCase):
    def test_builds_python_c_snippet_command(self) -> None:
        cmd = bridge_utils.db_read_cmd(
            "/opt/venvs/v1/bin/python",
            ["/opt/venvs/v1/logs", "~/logs"],
            "2026-09-18T00:00:00+00:00",
            300,
        )
        self.assertIn("/opt/venvs/v1/bin/python", cmd)
        self.assertIn("-c", cmd)
        self.assertIn("runspec_runs.db", cmd)  # the embedded snippet
        self.assertIn("mode=ro", cmd)
        # The JSON arg carries the dirs + window + limit.
        self.assertIn("~/logs", cmd)
        self.assertIn("2026-09-18T00:00:00+00:00", cmd)


# ── Phase 3 part 2b: the live "Running now" read ────────────────────────────────


class TestDbRowToRunning(unittest.TestCase):
    def test_maps_running_shape(self) -> None:
        r = bridge_utils.db_row_to_running(
            _db_row(
                run_id="live-1",
                runnable="db-migrate",
                status="running",
                exit_code=None,
                end_ts=None,
                duration_ms=None,
                command="db migrate",
                agent=1,
                skill="nightly",
                trigger="schedule",
                pid=4242,
            ),
            "v1",
            "prod-1",
        )
        self.assertEqual(r["id"], hashlib.md5(b"live-1db-migrate").hexdigest()[:12])
        self.assertEqual(r["runId"], "live-1")
        self.assertEqual(r["runnable"], "db-migrate")
        self.assertEqual(r["group"], "v1")
        self.assertEqual(r["host"], "prod-1")
        self.assertEqual(r["startTs"], "2026-09-25T09:59:59+00:00")  # start_ts, not end
        self.assertEqual(r["pid"], 4242)
        self.assertEqual(r["commandPath"], ["db", "migrate"])
        self.assertEqual(r["initiatedBy"], "llm")  # agent flag
        self.assertEqual(r["runbook"], "nightly")  # index `skill` column
        self.assertEqual(r["trigger"], "schedule")
        self.assertEqual(r["args"], {"target": "prod"})
        # A running run has no finished-run fields — the strip renders elapsed from
        # startTs instead, so these must be absent (not a false exit 0 / 0ms).
        self.assertNotIn("exitCode", r)
        self.assertNotIn("durationMs", r)
        self.assertNotIn("ts", r)

    def test_bad_args_json_is_empty(self) -> None:
        r = bridge_utils.db_row_to_running(
            _db_row(status="running", args="not json"), "core", "local"
        )
        self.assertEqual(r["args"], {})


def _write_running(
    logs: Path, run_id: str, runnable: str, *, alive: bool = True
) -> None:
    """A ``running`` row (begin_run only, no finalize). ``alive`` uses our own live
    pid + proc_start token; otherwise a dead pid so the liveness filter drops it."""
    runsdb.begin_run(
        logs,
        {
            "run_id": run_id,
            "runnable": runnable,
            "status": "running",
            "start_ts": "2026-09-25T10:00:00+00:00",
            "user": "alice",
            "agent": False,
            "pid": os.getpid() if alive else 999999999,
            "host": runsdb._hostname(),
            "proc_start": runsdb.proc_start_token() if alive else "",
            "log_file": f"{runnable}.x.{run_id}.log",
            "args": {"t": "prod"},
        },
    )


class TestRunningRunsLocal(unittest.TestCase):
    def _bridge_for(self, tmp: Path, rps: list[str]):
        b = _make_bridge()
        b._hosts = [{"name": "ws", "runspec_paths": rps}]
        return b

    def _run(self, b, tmp: Path):
        home = tmp / "home"
        home.mkdir(exist_ok=True)
        with patch.object(Path, "home", return_value=home):
            return b._running_runs_host("ws")

    def test_live_running_row_appears(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            tmp = Path(d)
            rp, logs = _venv_with_db(tmp, "v1")
            _write_running(logs, "live-1", "backup")
            rows = self._run(self._bridge_for(tmp, [rp]), tmp)
            row = next(r for r in rows if r["runId"] == "live-1")
            self.assertEqual(row["runnable"], "backup")
            self.assertEqual(row["group"], "v1")
            self.assertEqual(row["pid"], os.getpid())
            self.assertTrue(row["startTs"])

    def test_finished_row_not_running(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            tmp = Path(d)
            rp, logs = _venv_with_db(tmp, "v1")
            _write_db_run(logs, "done-1", "backup")  # begin + finish → completed
            rows = self._run(self._bridge_for(tmp, [rp]), tmp)
            self.assertNotIn("done-1", {r["runId"] for r in rows})

    def test_dead_pid_row_dropped(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            tmp = Path(d)
            rp, logs = _venv_with_db(tmp, "v1")
            _write_running(logs, "dead-1", "backup", alive=False)
            # runsdb.list_runs(running_only=True, live=True) liveness-filters a dead
            # pid on this host, so the orphan never reads as running.
            rows = self._run(self._bridge_for(tmp, [rp]), tmp)
            self.assertNotIn("dead-1", {r["runId"] for r in rows})

    def test_no_db_is_empty(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            tmp = Path(d)
            rp, _logs = _venv_with_db(tmp, "v1")  # logs dir but no DB written
            rows = self._run(self._bridge_for(tmp, [rp]), tmp)
            self.assertEqual(rows, [])  # index-only — no log-parse for running


class TestRunningRunsRemote(unittest.TestCase):
    def _bridge(self):
        b = _make_bridge()
        b._hosts = [
            {
                "name": "prod-1",
                "ssh": "u@h1",
                "runspec_paths": ["/opt/venvs/v1/bin/runspec"],
            }
        ]
        return b

    def test_reads_running_over_ssh(self) -> None:
        b = self._bridge()
        payload = {
            "/opt/venvs/v1/logs": {
                "has_db": True,
                "rows": [_db_row(run_id="rlive", runnable="backup", status="running")],
            },
            "~/logs": {"has_db": False, "rows": []},
        }

        def ssh(_t, cmd, **kw):
            # The running read uses the running snippet (selects status='running').
            self.assertIn("runspec_runs.db", cmd)
            self.assertIn("running", cmd)
            return (0, json.dumps(payload), "")

        with patch.object(b, "_ssh_run", side_effect=ssh):
            rows = b._running_runs_host("prod-1")
        row = next(r for r in rows if r["runId"] == "rlive")
        self.assertEqual(row["group"], "v1")
        self.assertEqual(row["runnable"], "backup")

    def test_snippet_failure_is_empty(self) -> None:
        b = self._bridge()

        def ssh(_t, cmd, **kw):
            return (0, "not json", "")  # unparseable → [] (best-effort, never raises)

        with patch.object(b, "_ssh_run", side_effect=ssh):
            self.assertEqual(b._running_runs_host("prod-1"), [])


class TestGetRunningRunsAggregation(unittest.TestCase):
    def test_merges_targets_newest_started_first(self) -> None:
        b = _make_bridge()

        def per_host(name: str):
            if name == "local":
                return [{"runId": "a", "startTs": "2026-09-25T10:00:00+00:00"}]
            return [{"runId": "b", "startTs": "2026-09-25T12:00:00+00:00"}]

        with (
            patch.object(b, "history_targets", return_value=["local", "prod-1"]),
            patch.object(b, "_running_runs_host", side_effect=per_host),
        ):
            rows = b.get_running_runs()
        # newest-started first (b at 12:00 before a at 10:00)
        self.assertEqual([r["runId"] for r in rows], ["b", "a"])


class TestDbRunningCmd(unittest.TestCase):
    def test_builds_running_snippet_command(self) -> None:
        cmd = bridge_utils.db_running_cmd(
            "/opt/venvs/v1/bin/python", ["/opt/venvs/v1/logs", "~/logs"], 200
        )
        self.assertIn("/opt/venvs/v1/bin/python", cmd)
        self.assertIn("-c", cmd)
        self.assertIn("runspec_runs.db", cmd)
        self.assertIn("mode=ro", cmd)
        self.assertIn("~/logs", cmd)
        # The running snippet has no `since` window (unlike db_read_cmd).
        self.assertIn("limit", cmd)


if __name__ == "__main__":
    unittest.main()

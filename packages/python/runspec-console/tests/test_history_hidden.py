"""
test_history_hidden.py — ``discoverable = false`` runnables stay out of the console.

A runnable whose runspec.toml says ``discoverable = false`` is dropped from the
agent/Forms surfaces (``_visible_runnables``) and its runs are left out of the
History tab on every read path *before* the row cap — so a job cron runs every
minute can't push everything else out — unless the tab asks for hidden runs.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runspec import runsdb

from runspec_console import bridge_utils
from runspec_console.bridge import Bridge
from runspec_console.history_cache import HistoryCache


def _make_bridge():
    with (
        patch("runspec_console.hosts.load_hosts", return_value=[]),
        patch.object(Bridge, "_start_refresh_watcher"),
    ):
        b = Bridge()
    b._ssh_pool = None
    return b


def _runnable(name: str, host: str, *, hidden: bool, group: str = "v1") -> dict:
    spec = {"discoverable": False} if hidden else {}
    return {"name": name, "host": host, "group": group, "args": [], "rawSpec": spec}


class TestHiddenNames(unittest.TestCase):
    def test_hidden_on_host_only(self) -> None:
        cache = [
            _runnable("heartbeat", "ws", hidden=True),
            _runnable("backup", "ws", hidden=False),
            _runnable("heartbeat", "other", hidden=False),
        ]
        self.assertEqual(bridge_utils.hidden_runnable_names(cache, "ws"), {"heartbeat"})
        self.assertEqual(bridge_utils.hidden_runnable_names(cache, "other"), set())

    def test_visible_in_another_venv_wins(self) -> None:
        cache = [
            _runnable("check", "ws", hidden=True, group="v1"),
            _runnable("check", "ws", hidden=False, group="v2"),
        ]
        self.assertEqual(bridge_utils.hidden_runnable_names(cache, "ws"), set())

    def test_discoverable_flag(self) -> None:
        self.assertFalse(
            bridge_utils.runnable_discoverable(_runnable("a", "ws", hidden=True))
        )
        self.assertTrue(
            bridge_utils.runnable_discoverable(_runnable("a", "ws", hidden=False))
        )
        self.assertTrue(bridge_utils.runnable_discoverable({"name": "a"}))  # no rawSpec


class TestVisibleRunnables(unittest.TestCase):
    def test_hidden_runnable_dropped_from_agent_and_forms(self) -> None:
        b = _make_bridge()
        b._runnables_cache = [
            _runnable("heartbeat", "ws", hidden=True),
            _runnable("backup", "ws", hidden=False),
        ]
        with patch(
            "runspec_console.config.read_tool_visibility",
            return_value={"hidden_from_agent": [], "hidden_from_all": []},
        ):
            for surface in ("agent", "forms"):
                names = [r["name"] for r in b._visible_runnables(surface)]
                self.assertEqual(names, ["backup"], surface)


def _venv(tmp: Path, name: str) -> tuple[str, Path]:
    venv = tmp / name
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "runspec").write_text("#!/bin/sh\n")
    logs = venv / "logs"
    logs.mkdir()
    return str(venv / "bin" / "runspec"), logs


def _db_run(logs: Path, run_id: str, runnable: str, minute: int) -> None:
    ts = f"2026-09-25T{10 + minute // 60:02d}:{minute % 60:02d}:00+00:00"
    runsdb.begin_run(
        logs,
        {
            "run_id": run_id,
            "runnable": runnable,
            "status": "running",
            "start_ts": ts,
            "user": "alice",
            "agent": False,
            "pid": os.getpid(),
            "host": runsdb._hostname(),
            "proc_start": runsdb.proc_start_token(),
            "log_file": f"{runnable}.log",
            "args": {},
        },
    )
    runsdb.finish_run(logs, run_id, exit_code=0, duration_ms=5, status="completed")


def _log_run(logs: Path, run_id: str, runnable: str, ts: str) -> None:
    line = {
        "ts": ts,
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
    with (logs / f"{runnable}.log").open("a") as fh:
        fh.write(json.dumps(line) + "\n")


class TestLocalHistory(unittest.TestCase):
    def _bridge(self, rp: str) -> Bridge:
        b = _make_bridge()
        b._hosts = [{"name": "ws", "runspec_paths": [rp]}]
        b._runnables_cache = [
            _runnable("heartbeat", "ws", hidden=True),
            _runnable("backup", "ws", hidden=False),
        ]
        return b

    def test_db_read_skips_hidden_before_the_cap(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            tmp = Path(d)
            rp, logs = _venv(tmp, "v1")
            _db_run(logs, "old-backup", "backup", 0)
            # More hidden runs than the History cap, all newer than the backup.
            for i in range(Bridge._HISTORY_CAP + 20):
                _db_run(logs, f"hb-{i}", "heartbeat", 1 + i)
            b = self._bridge(rp)
            home = tmp / "home"
            home.mkdir()
            with patch.object(Path, "home", return_value=home):
                rows = b.get_history_slim("ws", since_days=None)
                shown = b.get_history_slim("ws", since_days=None, include_hidden=True)
            self.assertEqual([r["runnable"] for r in rows], ["backup"])
            self.assertEqual(len(shown), Bridge._HISTORY_CAP)
            self.assertTrue(all(r["runnable"] == "heartbeat" for r in shown))

    def test_log_parse_skips_hidden(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            tmp = Path(d)
            rp, logs = _venv(tmp, "v1")  # no runs index → log parse
            _log_run(logs, "b1", "backup", "2026-09-25T09:00:00+00:00")
            for i in range(30):
                _log_run(
                    logs, f"hb-{i}", "heartbeat", f"2026-09-25T10:{i:02d}:00+00:00"
                )
            b = self._bridge(rp)
            home = tmp / "home"
            home.mkdir()
            with patch.object(Path, "home", return_value=home):
                rows = b.get_history_slim("ws", since_days=None)
                shown = b.get_history_slim("ws", since_days=None, include_hidden=True)
            self.assertEqual({r["runnable"] for r in rows}, {"backup"})
            self.assertEqual(len(shown), 31)


class TestRemoteDbSnippet(unittest.TestCase):
    def test_snippet_excludes_in_sql(self) -> None:
        """Run the real remote snippet against a real index: the hidden runnable is
        left out before LIMIT, so the older visible run still comes back."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            logs = Path(d) / "logs"
            logs.mkdir()
            _db_run(logs, "old-backup", "backup", 0)
            for i in range(5):
                _db_run(logs, f"hb-{i}", "heartbeat", 1 + i)
            cmd = bridge_utils.db_read_cmd(
                sys.executable, [str(logs)], None, 3, exclude=["heartbeat"]
            )
            out = subprocess.run(
                shlex.split(cmd), capture_output=True, text=True, check=True
            ).stdout
            rows = json.loads(out)[str(logs)]["rows"]
            self.assertEqual([r["runnable"] for r in rows], ["backup"])

    def test_no_exclude_keeps_the_old_payload(self) -> None:
        cmd = bridge_utils.db_read_cmd("/v/bin/python", ["/v/logs"], None, 10)
        self.assertNotIn("exclude", shlex.split(cmd)[-1])

    def test_remote_db_rows_and_fallback_filtered(self) -> None:
        b = _make_bridge()
        b._hosts = [
            {"name": "prod-1", "ssh": "u@h1", "runspec_paths": ["/opt/v1/bin/runspec"]}
        ]
        b._runnables_cache = [_runnable("heartbeat", "prod-1", hidden=True)]
        seen: list[str] = []
        row = {
            "run_id": "r1",
            "runnable": "backup",
            "command": "",
            "status": "completed",
            "exit_code": 0,
            "start_ts": "2026-09-25T09:00:00+00:00",
            "end_ts": "2026-09-25T09:00:01+00:00",
            "duration_ms": 1,
            "args": "{}",
        }
        hb = {**row, "run_id": "r2", "runnable": "heartbeat"}
        result = {
            "/opt/v1/logs": {"has_db": True, "rows": [row], "pending": [hb]},
            "~/logs": {"has_db": False, "rows": []},
        }

        def ssh(_t, cmd, **kw):
            seen.append(cmd)
            if "runspec_runs.db" in cmd:
                return (0, json.dumps(result), "")
            return (0, "", "")

        with (
            patch.object(b, "_ssh_run", side_effect=ssh),
            patch.object(b, "_history_from_cache", return_value=None),
        ):
            rows = b.get_history_slim("prod-1", since_days=None)
        self.assertEqual([r["runnable"] for r in rows], ["backup"])
        self.assertIn("heartbeat", next(c for c in seen if "runspec_runs.db" in c))


class TestHistoryCacheExclude(unittest.TestCase):
    def test_read_excludes_before_limit(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            c = HistoryCache(Path(d) / "cache.db")

            def rec(run_id: str, runnable: str, ts: str) -> dict:
                return {
                    "runId": run_id,
                    "runnable": runnable,
                    "ts": ts,
                    "kind": "runnable",
                }

            c.replace(
                "h",
                "/d",
                [rec("a", "backup", "2026-09-25T09:00:00+00:00")]
                + [
                    rec(f"hb{i}", "heartbeat", f"2026-09-25T10:{i:02d}:00+00:00")
                    for i in range(5)
                ],
            )
            c.set_pending(
                "h", "/d", [rec("p", "heartbeat", "2026-09-25T11:00:00+00:00")]
            )
            got = c.read("h", since_date=None, limit=2, exclude=["heartbeat"])
            self.assertEqual([r["runId"] for r in got], ["a"])
            self.assertEqual(len(c.read("h", since_date=None, limit=2)), 2)
            c.close()


class TestRunningStrip(unittest.TestCase):
    def test_hidden_running_rows_left_out(self) -> None:
        b = _make_bridge()
        b._runnables_cache = [_runnable("heartbeat", "local", hidden=True)]
        live = [
            {"runnable": "heartbeat", "startTs": "2"},
            {"runnable": "backup", "startTs": "1"},
        ]
        with (
            patch.object(b, "history_targets", return_value=["local"]),
            patch.object(b, "_running_runs_host", return_value=live),
        ):
            self.assertEqual([r["runnable"] for r in b.get_running_runs()], ["backup"])
            self.assertEqual(len(b.get_running_runs(include_hidden=True)), 2)


if __name__ == "__main__":
    unittest.main()

"""
test_history_cache.py — the History tab's local cache of remote hosts' runs
(docs/design/history-incremental-pull.md).

The remote pull snippet is exercised for real: the fake ``_ssh_run`` runs the
``{python} -c <snippet> <json>`` command locally (with the current interpreter and
a temp HOME) against runs indexes written by core ``runsdb`` — so the tests cover
the snippet ↔ core contract, not a mock of it.
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


def _now(days_ago: int = 0) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
        timespec="seconds"
    )


def _run(
    log_dir: Path, run_id: str, *, status: str = "completed", days_ago: int = 0, **over
) -> None:
    row = {
        "run_id": run_id,
        "runnable": over.pop("runnable", "backup"),
        "command": "",
        "status": status,
        "start_ts": _now(days_ago),
        "user": "alice",
        "agent": False,
        "pid": 1,
        "host": "remote-box",
        "log_file": f"{run_id}.log",
        "args": {},
    }
    row.update(over)
    runsdb.begin_run(log_dir, row)
    if status != "running":
        con = runsdb._connect(runsdb.db_path(log_dir))
        assert con is not None
        try:
            con.execute(
                "UPDATE runs SET end_ts=?, exit_code=? WHERE run_id=?",
                (row["start_ts"], 0, run_id),
            )
            con.commit()
        finally:
            con.close()


def _run_snippet(cmd: str, home: Path) -> str:
    argv = shlex.split(cmd)
    argv[0] = sys.executable  # the remote venv's python → this interpreter
    # ~ is HOME on POSIX, USERPROFILE on Windows — point both at the fake home.
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    return subprocess.run(
        argv, capture_output=True, text=True, env=env, check=True
    ).stdout


class TestSnippetParity(unittest.TestCase):
    """The stdlib snippet and core's runsdb.changes_since give the same answer."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.logs = self.tmp / "v1" / "logs"
        self.logs.mkdir(parents=True)

    def _both(self, cursor=None, limit=2000, window_start=None):
        cursor = cursor or {}
        cmd = bridge_utils.db_sync_cmd(
            "python", {str(self.logs): cursor}, limit=limit, window_start=window_start
        )
        snip = json.loads(_run_snippet(cmd, self.tmp))[str(self.logs)]
        core = runsdb.changes_since(
            self.logs,
            db_id=cursor.get("db_id"),
            seq=cursor.get("seq"),
            limit=limit,
            window_start=window_start,
        )
        return snip, core

    def _same(self, snip, core):
        for k in ("mode", "db_id", "seq", "deleted", "more"):
            self.assertEqual(snip.get(k), core.get(k), k)
        self.assertEqual(
            [(r["run_id"], r["seq"]) for r in snip["rows"]],
            [(r["run_id"], r["seq"]) for r in core["rows"]],
        )

    def test_none_legacy_reset_delta_and_pages(self) -> None:
        snip, _ = self._both()
        self.assertEqual(snip["mode"], "none")
        for i in range(5):
            _run(self.logs, f"r{i}")
        snip, core = self._both(limit=2)
        self._same(snip, core)
        self.assertEqual((snip["mode"], snip["more"]), ("reset", True))
        cursor = {"db_id": snip["db_id"], "seq": snip["seq"]}
        runsdb.prune_now(self.logs, retention_days=0, max_rows=3)
        _run(self.logs, "r5")
        snip, core = self._both(cursor, limit=50, window_start=_now(30))
        self._same(snip, core)
        self.assertEqual(snip["mode"], "delta")
        self.assertTrue(snip["deleted"])

    def test_legacy_index(self) -> None:
        import sqlite3

        con = sqlite3.connect(str(runsdb.db_path(self.logs)))
        con.executescript(runsdb._DDL)
        con.commit()
        con.close()
        snip, core = self._both()
        self.assertEqual((snip["mode"], core["mode"]), ("legacy", "legacy"))


class TestHistoryCacheStore(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "history_cache.db"
        self.cache = HistoryCache(self.path)

    def tearDown(self) -> None:
        self.cache.close()

    def _rec(self, run_id: str, ts: str = "2026-09-28T10:00:00+00:00") -> dict:
        return {
            "id": run_id,
            "runId": run_id,
            "ts": ts,
            "runnable": "backup",
            "kind": "runnable",
        }

    def test_reset_delta_delete_and_read(self) -> None:
        c = self.cache
        self.assertTrue(
            c.apply(
                "h",
                "/d",
                {"mode": "reset", "db_id": "x", "seq": 2},
                [self._rec("a"), self._rec("b", "2026-09-27T00:00:00+00:00")],
                [],
            )
        )
        self.assertEqual(
            [r["runId"] for r in c.read("h", since_date=None, limit=10)], ["a", "b"]
        )
        self.assertEqual(c.cursors("h"), {"/d": {"db_id": "x", "seq": 2}})
        c.apply("h", "/d", {"mode": "delta", "db_id": "x", "seq": 3}, [], ["a"])
        self.assertEqual(
            [r["runId"] for r in c.read("h", since_date=None, limit=10)], ["b"]
        )
        self.assertEqual(c.read("h", since_date="2026-09-28", limit=10), [])
        # a reset drops the dir's old records first
        c.apply(
            "h", "/d", {"mode": "reset", "db_id": "y", "seq": 1}, [self._rec("c")], []
        )
        self.assertEqual(
            [r["runId"] for r in c.read("h", since_date=None, limit=10)], ["c"]
        )

    def test_replace_trim_retain_and_status(self) -> None:
        c = self.cache
        self.assertTrue(
            c.replace(
                "h",
                "/legacy",
                [self._rec("a"), self._rec("old", "2020-01-01T00:00:00+00:00")],
            )
        )
        self.assertFalse(
            c.replace(
                "h",
                "/legacy",
                [self._rec("a"), self._rec("old", "2020-01-01T00:00:00+00:00")],
            )
        )
        self.assertEqual(c.cursors("h"), {"/legacy": {"db_id": None, "seq": 0}})
        c.trim("2026-01-01T00:00:00+00:00")
        self.assertEqual(
            [r["runId"] for r in c.read("h", since_date=None, limit=10)], ["a"]
        )
        self.assertFalse(c.is_primed("h"))
        c.mark("h")
        self.assertTrue(c.is_primed("h"))
        c.mark("h", error="boom")
        st = c.status("h")
        self.assertTrue(
            st["synced_at"] and st["error"] == "boom"
        )  # keeps the last good sync
        c.retain_hosts({"other"})
        self.assertEqual(c.read("h", since_date=None, limit=10), [])
        self.assertFalse(c.is_primed("h"))

    def test_drop_dirs(self) -> None:
        c = self.cache
        c.replace("h", "/a", [self._rec("a")])
        c.replace("h", "/b", [self._rec("b")])
        self.assertTrue(c.drop_dirs("h", {"/a"}))
        self.assertEqual(set(c.cursors("h")), {"/a"})

    def test_corrupt_file_is_rebuilt(self) -> None:
        self.cache.close()
        self.path.write_bytes(b"this is not a sqlite database" * 100)
        c = HistoryCache(self.path)
        c.replace("h", "/d", [self._rec("a")])
        self.assertEqual(
            [r["runId"] for r in c.read("h", since_date=None, limit=10)], ["a"]
        )
        c.close()


class _PullFixture(unittest.TestCase):
    """A Bridge with one remote host whose fake SSH runs the real pull snippet
    against core-written indexes. No tests of its own."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        (self.home / "logs").mkdir(parents=True)
        self.v1 = self.tmp / "v1"
        self.logs = self.v1 / "logs"
        self.logs.mkdir(parents=True)
        with (
            patch("runspec_console.hosts.load_hosts", return_value=[]),
            patch.object(Bridge, "_start_refresh_watcher"),
        ):
            b = Bridge()
        b._ssh_pool = None
        b._hosts = [
            {
                "name": "prod-1",
                "ssh": "u@h1",
                # Remote hosts are POSIX: the layout reads these with PurePosixPath,
                # so give it forward slashes (Windows accepts them for the local
                # files the fake SSH actually opens).
                "runspec_paths": [(self.v1 / "bin" / "runspec").as_posix()],
            }
        ]
        b._history_cache_obj = HistoryCache(self.tmp / "cache.db")
        self.events: list[tuple[str, dict]] = []
        b._dispatch = lambda ev, d: self.events.append((ev, d))  # type: ignore[method-assign]
        self.b = b
        self.ssh_calls: list[str] = []
        self.ssh_fail = False

    def tearDown(self) -> None:
        cache = self.b._history_cache_obj
        if cache is not None:
            cache.close()

    def ssh(self, _target, cmd, **_kw):
        self.ssh_calls.append(cmd)
        if self.ssh_fail:
            raise OSError("connection refused")
        if "RUNSPEC_LOG" in cmd:  # log-parse stream for dirs without an index
            return (0, "", "")
        return (0, _run_snippet(cmd, self.home), "")

    def _slim(self, since_days=7):
        with (
            patch.object(self.b, "_ssh_run", side_effect=self.ssh),
            patch.object(self.b, "_kick_history_pull") as kick,
            patch("runspec_console.config.read_history", return_value={"cache": True}),
        ):
            rows = self.b.get_history_slim("prod-1", since_days=since_days)
        return rows, kick

    def _pull(self):
        with (
            patch.object(self.b, "_ssh_run", side_effect=self.ssh),
            patch("runspec_console.config.read_history", return_value={"cache": True}),
        ):
            return self.b._history_pull(self.b._host_entry("prod-1"))


class TestHistoryPull(_PullFixture):
    """End to end: Bridge ↔ fake SSH running the real snippet ↔ core-written DBs."""

    def test_first_read_pulls_then_serves_from_cache(self) -> None:
        _run(self.logs, "a")
        _run(self.logs, "live", status="running")
        rows, kick = self._slim()
        self.assertEqual([r["runId"] for r in rows], ["a"])  # finished runs only
        self.assertEqual(rows[0]["group"], "v1")
        kick.assert_not_called()  # first read pulled in line
        # a new run → the next pull delivers only it and says the host changed
        _run(self.logs, "b")
        self.events.clear()
        self.ssh_calls.clear()
        self.assertTrue(self._pull())
        self.assertEqual(self.events, [("runspec:history_updated", {"host": "prod-1"})])
        self.assertEqual(len(self.ssh_calls), 1)  # one sync round-trip, nothing else
        # later reads come from the cache and kick a background pull
        self.ssh_calls.clear()
        rows, kick = self._slim()
        self.assertEqual({r["runId"] for r in rows}, {"a", "b"})
        self.assertEqual(self.ssh_calls, [])
        kick.assert_called_once_with("prod-1")

    def test_quiet_pull_emits_nothing(self) -> None:
        _run(self.logs, "a")
        self._slim()
        self.events.clear()
        self.assertTrue(self._pull())
        self.assertEqual(self.events, [])

    def test_remote_prune_reaches_the_cache(self) -> None:
        _run(self.logs, "a")
        _run(self.logs, "b")
        self._slim()
        runsdb.prune_now(self.logs, retention_days=0, max_rows=1)
        self._pull()
        rows, _ = self._slim()
        self.assertEqual(len(rows), 1)

    def test_host_without_v4_index_reads_directly(self) -> None:
        import sqlite3

        con = sqlite3.connect(str(runsdb.db_path(self.logs)))
        con.executescript(runsdb._DDL)
        con.execute(
            "INSERT INTO runs (run_id, runnable, status, start_ts, end_ts) VALUES ('old', 'backup', 'completed', ?, ?)",
            (_now(), _now()),
        )
        con.commit()
        con.close()
        with patch.object(
            self.b, "_slim_history_db_remote", return_value=[{"runId": "direct"}]
        ) as direct:
            rows, _ = self._slim()
        direct.assert_called_once()
        self.assertEqual(rows, [{"runId": "direct"}])
        self.assertFalse(self.b._history_cache().is_primed("prod-1"))

    def test_mixed_host_caches_legacy_and_unindexed_dirs_as_snapshots(self) -> None:
        import sqlite3

        # v1 is v4; v2 is a pre-v4 index read directly; ~/logs has no index
        _run(self.logs, "v4row")
        v2 = self.tmp / "v2"
        (v2 / "logs").mkdir(parents=True)
        con = sqlite3.connect(str(runsdb.db_path(v2 / "logs")))
        con.executescript(runsdb._DDL)
        con.execute(
            "INSERT INTO runs (run_id, runnable, status, start_ts, end_ts) VALUES ('v3row', 'deploy', 'completed', ?, ?)",
            (_now(), _now()),
        )
        con.commit()
        con.close()
        self.b._hosts[0]["runspec_paths"].append((v2 / "bin" / "runspec").as_posix())
        rows, _ = self._slim()
        self.assertEqual({r["runId"] for r in rows}, {"v4row", "v3row"})
        self.assertEqual(next(r for r in rows if r["runId"] == "v3row")["group"], "v2")

    def test_wider_windows_and_cache_off_bypass(self) -> None:
        _run(self.logs, "a")
        with patch.object(self.b, "_slim_history_db_remote", return_value=[]) as direct:
            self._slim(since_days=90)
            self._slim(since_days=0)
            with (
                patch(
                    "runspec_console.config.read_history", return_value={"cache": False}
                ),
                patch.object(self.b, "_ssh_run", side_effect=self.ssh),
            ):
                self.b.get_history_slim("prod-1", since_days=7)
        self.assertEqual(direct.call_count, 3)

    def test_failed_pull_keeps_cache_and_reports(self) -> None:
        _run(self.logs, "a")
        self._slim()
        self.ssh_fail = True
        self.assertTrue(self._pull())  # still served from the cache
        with patch("runspec_console.config.read_history", return_value={"cache": True}):
            status = self.b.history_sync_status()
        self.assertIn("connection refused", status["prod-1"]["error"])
        self.assertTrue(status["prod-1"]["cached"] and status["prod-1"]["synced_at"])
        rows, _ = self._slim()
        self.assertEqual([r["runId"] for r in rows], ["a"])

    def test_run_finishing_mid_pull_queues_one_more_pull(self) -> None:
        _run(self.logs, "a")
        self._slim()  # primes the cache
        calls: list[bool] = []

        def fake_pull(entry, *, claimed=False, force=False):
            calls.append(force)
            if len(calls) == 1:  # a run finishes while this pull is under way
                self.b._kick_history_pull("prod-1", force=True)
            return True

        with (
            patch.object(self.b, "_history_pull", side_effect=fake_pull),
            patch("runspec_console.config.read_history", return_value={"cache": True}),
        ):
            self.b._kick_history_pull("prod-1", force=True)
            for t in [
                t
                for t in __import__("threading").enumerate()
                if t.name == "history-pull-prod-1"
            ]:
                t.join(5)
        self.assertEqual(calls, [True, True])
        self.assertEqual(self.b._history_sync_state()["pulling"], set())

    def test_removed_host_is_forgotten(self) -> None:
        _run(self.logs, "a")
        self._slim()
        self.b._hosts = []
        self.b.history_targets()
        self.assertFalse(self.b._history_cache().is_primed("prod-1"))


if __name__ == "__main__":
    unittest.main()


class TestDbRowExtra(unittest.TestCase):
    def test_v5_columns_map_into_extra(self) -> None:
        rec = bridge_utils.db_row_to_record(
            {
                "run_id": "r",
                "runnable": "backup",
                "status": "failed",
                "exit_code": 1,
                "start_ts": _now(),
                "autonomy": "confirm",
                "skill": "rb1",
                "command": "sync all",
                "events": '{"ERROR": 2, "INFO": 5}',
                "exception": '{"type": "OSError", "message": "disk full"}',
                "args": '{"target": "prod"}',
            },
            "v1",
            "prod-1",
        )
        ex = rec["extra"]
        self.assertEqual(ex["events"], {"ERROR": 2, "INFO": 5})
        self.assertEqual(ex["exception"], {"type": "OSError", "message": "disk full"})
        self.assertEqual((ex["autonomy"], ex["runbook"]), ("confirm", "rb1"))
        self.assertEqual(ex["command_path"], ["sync", "all"])
        self.assertEqual(ex["invocation_args"], {"target": "prod"})

    def test_pre_v5_row_is_unknown_and_empty_is_empty(self) -> None:
        old = bridge_utils.db_row_to_record({"run_id": "r", "runnable": "x"}, "v", "h")
        self.assertIsNone(old["extra"]["events"])
        self.assertIsNone(old["extra"]["exception"])
        quiet = bridge_utils.db_row_to_record(
            {"run_id": "r", "runnable": "x", "events": "{}"}, "v", "h"
        )
        self.assertEqual(quiet["extra"]["events"], {})


class TestCacheVersion(unittest.TestCase):
    def test_an_older_cache_is_rebuilt(self) -> None:
        import sqlite3

        path = Path(tempfile.mkdtemp()) / "history_cache.db"
        con = sqlite3.connect(str(path))
        con.execute(
            "CREATE TABLE recs (host TEXT, dir TEXT, key TEXT, ts TEXT, rec TEXT, PRIMARY KEY (host, dir, key))"
        )
        con.execute(
            "INSERT INTO recs VALUES ('h', '/d', 'a', '2026-09-28', '{\"runId\": \"a\"}')"
        )
        con.execute("PRAGMA user_version=1")
        con.commit()
        con.close()
        c = HistoryCache(path)
        self.assertEqual(
            c.read("h", since_date=None, limit=10), []
        )  # old-shape records gone
        c.close()


class TestAnalyticsFromCache(_PullFixture):
    """get_analytics aggregates a cachable remote host from the History cache."""

    def _finish(self, run_id: str, *, exit_code=0, events=None, exception=None) -> None:
        runsdb.begin_run(
            self.logs,
            {
                "run_id": run_id,
                "runnable": "backup",
                "status": "running",
                "start_ts": _now(),
                "host": "remote-box",
            },
        )
        runsdb.finish_run(
            self.logs,
            run_id,
            exit_code=exit_code,
            duration_ms=10,
            status="completed" if exit_code == 0 else "failed",
            events=events,
            exception=exception,
        )

    def _analytics(self, since_days=30, runnable=None):
        self.b._connected_cache = {"prod-1": True}
        with (
            patch.object(self.b, "_ssh_run", side_effect=self.ssh),
            patch.object(self.b, "_kick_history_pull"),
            patch.object(self.b, "_collect_host_records", return_value=[]) as logs,
            patch("runspec_console.config.read_history", return_value={"cache": True}),
        ):
            out = self.b.get_analytics(["prod-1"], since_days, runnable)
        return out, logs

    def test_aggregates_events_and_exceptions_from_the_cache(self) -> None:
        self._finish("ok", events={"INFO": 3, "WARNING": 1})
        self._finish(
            "bad",
            exit_code=1,
            events={"ERROR": 2},
            exception={"type": "OSError", "message": "disk full"},
        )
        out, logs = self._analytics()
        logs.assert_not_called()  # no log streaming for a cached host
        self.assertEqual((out["totalRuns"], out["failureCount"]), (2, 1))
        [bucket_events] = [b["events"] for b in out["buckets"] if b["failure"]]
        self.assertEqual(bucket_events["ERROR"], 2)
        self.assertEqual(out["exceptions"][0]["exception"], "OSError: disk full")
        self.assertEqual(out["eventsUnknown"], [])
        # the second open is served without SSH (from the cache)
        self.ssh_calls.clear()
        self.b._analytics_cache.clear()
        self._analytics()
        self.assertEqual(self.ssh_calls, [])

    def test_rows_from_an_older_runspec_are_flagged(self) -> None:
        self._finish("old")  # finish_run without events → NULL (unknown)
        out, _ = self._analytics()
        self.assertEqual(out["eventsUnknown"], ["prod-1"])

    def test_window_is_capped_at_the_cache_window(self) -> None:
        from datetime import date, timedelta

        self._finish("a")
        out, _ = self._analytics(since_days=90)
        self.assertEqual(out["since"], (date.today() - timedelta(days=29)).isoformat())

    def test_runnable_filter_and_uncachable_host_fallback(self) -> None:
        self._finish("a")
        out, _ = self._analytics(runnable="deploy")
        self.assertEqual(out["totalRuns"], 0)
        # a host with no v4 index is parsed from its logs as before
        with patch.object(self.b, "_cached_records", return_value=None):
            _, logs = self._analytics()
        logs.assert_called_once()

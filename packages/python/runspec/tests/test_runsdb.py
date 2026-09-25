"""Tests for the per-venv runs index (runspec.runsdb).

Module-level behaviour: schema/lifecycle (begin → finish UPSERT), orphan reaping
(pid-liveness + age), the read-time liveness filter, ring-buffer prune, reindex
from run_summary records, group-writable perms (chmod-before-WAL), the mode=ro
read, and the best-effort "never raises" contract.
"""

from __future__ import annotations

import os
import stat as statmod
from pathlib import Path

import pytest

from runspec import runsdb


@pytest.fixture
def logs_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    os.chmod(d, 0o2775)  # emulate create-venv's shared setgid logs dir
    return d


def _row(**over):
    base = {
        "run_id": "r1",
        "runnable": "backup",
        "command": "",
        "status": "running",
        "start_ts": "2026-09-25T10:00:00+00:00",
        "user": "alice",
        "agent": False,
        "trigger": "",
        "skill": "",
        "autonomy": "confirm",
        "pid": os.getpid(),
        "host": runsdb._hostname(),
        "proc_start": runsdb.proc_start_token(),
        "log_file": "backup.x.r1.log",
        "args": {"target": "prod"},
    }
    base.update(over)
    return base


class TestLifecycle:
    def test_begin_creates_running_row(self, logs_dir):
        runsdb.begin_run(logs_dir, _row())
        rows = runsdb.list_runs(logs_dir, live=False)
        assert [r["run_id"] for r in rows] == ["r1"]
        assert rows[0]["status"] == "running"
        assert rows[0]["args"] == '{"target": "prod"}'  # dict serialised to JSON

    def test_finish_upserts_completed(self, logs_dir):
        runsdb.begin_run(logs_dir, _row())
        runsdb.finish_run(logs_dir, "r1", exit_code=0, duration_ms=1234, status="completed")
        r = runsdb.list_runs(logs_dir, live=False)[0]
        assert (r["status"], r["exit_code"], r["duration_ms"]) == ("completed", 0, 1234)

    def test_finish_without_begin_still_records(self, logs_dir):
        # A very short run whose start row lost the race is still recorded whole.
        runsdb.finish_run(logs_dir, "r-late", exit_code=1, duration_ms=5, status="failed")
        rows = {r["run_id"]: r for r in runsdb.list_runs(logs_dir, live=False)}
        assert rows["r-late"]["status"] == "failed"


class TestReapAndLiveness:
    def test_dead_pid_reaps_to_crashed(self, logs_dir):
        runsdb.begin_run(logs_dir, _row(run_id="dead", pid=999999999, proc_start=""))
        assert runsdb.reap_now(logs_dir) == 1
        r = [x for x in runsdb.list_runs(logs_dir, live=False) if x["run_id"] == "dead"][0]
        assert r["status"] == "crashed"

    def test_live_pid_stays_running(self, logs_dir):
        runsdb.begin_run(logs_dir, _row(run_id="live"))  # our own live pid + token
        runsdb.reap_now(logs_dir)
        r = [x for x in runsdb.list_runs(logs_dir, live=False) if x["run_id"] == "live"][0]
        assert r["status"] == "running"

    def test_read_liveness_filter_downgrades_dead(self, logs_dir):
        # Even before the reaper runs, the read never *shows* a dead run as running.
        runsdb.begin_run(logs_dir, _row(run_id="dead", pid=999999999, proc_start=""))
        shown = runsdb.list_runs(logs_dir, running_only=True, live=True)
        assert not any(r["run_id"] == "dead" for r in shown)

    def test_age_ceiling_reaps_old_running(self, logs_dir):
        # A row from a live pid but far in the past is reaped by the age ceiling.
        runsdb.begin_run(logs_dir, _row(run_id="old", start_ts="2000-01-01T00:00:00+00:00"))
        runsdb.reap_now(logs_dir, cfg={"max_run_hours": 1})
        r = [x for x in runsdb.list_runs(logs_dir, live=False) if x["run_id"] == "old"][0]
        assert r["status"] == "crashed"


class TestPrune:
    def test_max_rows_keeps_newest(self, logs_dir):
        for i in range(5):
            runsdb.finish_run(logs_dir, f"r{i}", exit_code=0, duration_ms=1, status="completed")
        deleted = runsdb.prune_now(logs_dir, retention_days=0, max_rows=2)
        assert deleted == 3
        assert len(runsdb.list_runs(logs_dir, live=False)) == 2

    def test_retention_days_drops_old(self, logs_dir):
        runsdb.begin_run(logs_dir, _row(run_id="old", start_ts="2000-01-01T00:00:00+00:00"))
        runsdb.finish_run(logs_dir, "old", exit_code=0, duration_ms=1, status="completed")
        runsdb.finish_run(logs_dir, "new", exit_code=0, duration_ms=1, status="completed")
        runsdb.prune_now(logs_dir, retention_days=30, max_rows=0)
        ids = {r["run_id"] for r in runsdb.list_runs(logs_dir, live=False)}
        assert "old" not in ids and "new" in ids

    def test_running_rows_never_pruned(self, logs_dir):
        runsdb.begin_run(logs_dir, _row(run_id="live"))
        runsdb.prune_now(logs_dir, retention_days=0, max_rows=0)  # aggressive
        assert any(r["run_id"] == "live" for r in runsdb.list_runs(logs_dir, live=False))


class TestReindex:
    def test_reindex_from_run_summaries(self, logs_dir):
        n = runsdb.reindex(
            logs_dir,
            [
                {
                    "run_id": "h1",
                    "runnable": "backup",
                    "command_path": [],
                    "duration_ms": 900,
                    "exit_code": 0,
                    "agent": True,
                    "trigger": "schedule",
                    "skill": "",
                    "autonomy": "autonomous",
                    "exception": None,
                    "user": "svc",
                    "invocation_args": {},
                    "ts": "2026-09-20T08:00:00+00:00",
                },
                {
                    "run_id": "h2",
                    "runnable": "deploy",
                    "command_path": ["push"],
                    "duration_ms": 10,
                    "exit_code": 2,
                    "agent": False,
                    "exception": {"type": "ValueError"},
                    "ts": "2026-09-20T09:00:00+00:00",
                },
            ],
        )
        assert n == 2
        rows = {r["run_id"]: r for r in runsdb.list_runs(logs_dir, live=False)}
        assert rows["h1"]["status"] == "completed" and rows["h1"]["agent"] == 1
        assert rows["h1"]["trigger"] == "schedule"
        assert rows["h2"]["status"] == "failed" and rows["h2"]["command"] == "push"

    def test_reindex_skips_records_without_run_id(self, logs_dir):
        assert runsdb.reindex(logs_dir, [{"runnable": "x", "ts": "2026-01-01T00:00:00+00:00"}]) == 0


class TestFilters:
    def test_since_status_runnable_limit(self, logs_dir):
        runsdb.finish_run(logs_dir, "a", exit_code=0, duration_ms=1, status="completed")
        runsdb.begin_run(logs_dir, _row(run_id="b", runnable="deploy", start_ts="2026-09-25T12:00:00+00:00"))
        runsdb.finish_run(logs_dir, "b", exit_code=1, duration_ms=1, status="failed")
        assert [r["run_id"] for r in runsdb.list_runs(logs_dir, runnable="deploy", live=False)] == ["b"]
        assert [r["run_id"] for r in runsdb.list_runs(logs_dir, status="failed", live=False)] == ["b"]
        assert len(runsdb.list_runs(logs_dir, limit=1, live=False)) == 1


class TestPermsAndBestEffort:
    def test_chmod_before_wal_makes_db_and_sidecars_group_writable(self, logs_dir):
        runsdb.begin_run(logs_dir, _row())
        db = runsdb.db_path(logs_dir)
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db) + suffix)
            if p.exists():
                assert p.stat().st_mode & statmod.S_IWGRP, f"{p.name} not group-writable"

    def test_best_effort_on_unwritable_never_raises(self):
        # No exception must escape into the run for an unwritable location.
        runsdb.begin_run(Path("/proc/nonexistent-runsdb"), _row())
        runsdb.finish_run(Path("/proc/nonexistent-runsdb"), "r1", exit_code=0, duration_ms=1, status="completed")
        assert runsdb.list_runs(Path("/proc/nonexistent-runsdb")) == []

    def test_mode_ro_read_of_missing_db_is_empty(self, tmp_path):
        assert runsdb.list_runs(tmp_path / "no-logs-here") == []

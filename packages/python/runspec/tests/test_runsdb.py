"""Tests for the per-venv runs index (runspec.runsdb).

Module-level behaviour: schema/lifecycle (begin → finish UPSERT), orphan reaping
(pid-liveness + age), the read-time liveness filter, ring-buffer prune, reindex
from run_summary records, group-writable perms (chmod-before-WAL), the mode=ro
read, and the best-effort "never raises" contract.
"""

from __future__ import annotations

import os
import sqlite3
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


class TestPidLiveness:
    """The non-destructive existence probe behind reaping/liveness, and the
    Windows guard. ``os.kill(pid, 0)`` is a harmless existence probe on POSIX, but
    on Windows CPython maps it to ``TerminateProcess`` — it would *kill* the pid in
    every ``running`` row (the live console, an in-flight runnable) — so the
    Windows branch must never reach ``os.kill``."""

    def test_posix_probe_own_pid_alive(self):
        # Empty proc_start token → the existence-probe fallback; our own pid lives.
        assert runsdb._pid_alive(os.getpid(), "") is True

    def test_posix_probe_dead_pid(self):
        assert runsdb._pid_alive(999999999, "") is False

    def test_falsey_pid_never_alive(self):
        assert runsdb._pid_alive(0, "") is False
        assert runsdb._pid_alive(None, "tok") is False

    def test_windows_probe_never_calls_os_kill(self, monkeypatch):
        # Regression guard: on Windows the fallback routes through _win_pid_exists
        # (OpenProcess), never os.kill (which terminates the process on Windows).
        # `_on_windows` is patched rather than `os.name` — flipping os.name would
        # make pathlib pick WindowsPath and break unrelated code under test.
        seen: list[int] = []
        monkeypatch.setattr(runsdb, "_on_windows", lambda: True)
        monkeypatch.setattr(runsdb.os, "kill", lambda *a, **k: pytest.fail("os.kill must not run on Windows"))
        monkeypatch.setattr(runsdb, "_win_pid_exists", lambda pid: seen.append(pid) or True)
        assert runsdb._pid_alive(4321, "") is True  # empty token → probe path
        assert seen == [4321]

    def test_windows_liveness_filter_is_non_destructive(self, logs_dir, monkeypatch):
        # End-to-end: a running_only live read on a simulated Windows host must not
        # invoke the destructive os.kill for a row's pid, and keeps a live row.
        monkeypatch.setattr(runsdb, "_on_windows", lambda: True)
        monkeypatch.setattr(runsdb.os, "kill", lambda *a, **k: pytest.fail("liveness filter killed a pid"))
        monkeypatch.setattr(runsdb, "_win_pid_exists", lambda pid: True)  # process exists
        runsdb.begin_run(logs_dir, _row(run_id="win", pid=4321, proc_start=""))
        shown = runsdb.list_runs(logs_dir, running_only=True, live=True)
        assert any(r["run_id"] == "win" for r in shown)


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

    def test_begin_run_survives_without_getuid(self, logs_dir, monkeypatch):
        # Windows has no os.getuid; the group-writable chmod is POSIX-only and must
        # be skipped, not crash the fresh-DB create in begin_run. Before the guard,
        # os.getuid() raised AttributeError (not caught by suppress(OSError)), so the
        # start row was dropped and finish_run's fallback recorded an empty runnable.
        monkeypatch.delattr(runsdb.os, "getuid", raising=False)
        runsdb.begin_run(logs_dir, _row(run_id="w1", runnable="backup"))
        runsdb.finish_run(logs_dir, "w1", exit_code=0, duration_ms=1, status="completed")
        r = [x for x in runsdb.list_runs(logs_dir, live=False) if x["run_id"] == "w1"][0]
        assert r["runnable"] == "backup"  # start row landed → runnable preserved

    def test_best_effort_on_unwritable_never_raises(self):
        # No exception must escape into the run for an unwritable location.
        runsdb.begin_run(Path("/proc/nonexistent-runsdb"), _row())
        runsdb.finish_run(Path("/proc/nonexistent-runsdb"), "r1", exit_code=0, duration_ms=1, status="completed")
        assert runsdb.list_runs(Path("/proc/nonexistent-runsdb")) == []

    def test_mode_ro_read_of_missing_db_is_empty(self, tmp_path):
        assert runsdb.list_runs(tmp_path / "no-logs-here") == []


class TestPruneProvenance:
    """§8.1 — the log FILE's availability (log_pruned_at/by), stamped by the prune
    path so a row that outlives its log file can say when/by-whom, not the run
    outcome (which stays `status`)."""

    def test_fresh_db_has_prune_columns_null(self, logs_dir):
        runsdb.begin_run(logs_dir, _row(log_file="backup.x.r1.log"))
        r = runsdb.list_runs(logs_dir, live=False)[0]
        # v2 columns exist on a fresh DB and read NULL (log present).
        assert r["log_pruned_at"] is None
        assert r["log_pruned_by"] is None

    def test_mark_log_pruned_stamps_matching_rows(self, logs_dir):
        runsdb.begin_run(logs_dir, _row(run_id="r1", log_file="backup.x.r1.log"))
        runsdb.begin_run(logs_dir, _row(run_id="r2", log_file="other.x.r2.log"))
        n = runsdb.mark_log_pruned(logs_dir, ["backup.x.r1.log"], pruned_by="bob", pruned_at="2026-09-20T00:00:00+00:00")
        assert n == 1
        by_id = {r["run_id"]: r for r in runsdb.list_runs(logs_dir, live=False)}
        assert by_id["r1"]["log_pruned_by"] == "bob"
        assert by_id["r1"]["log_pruned_at"] == "2026-09-20T00:00:00+00:00"
        assert by_id["r2"]["log_pruned_at"] is None  # a different file — untouched

    def test_mark_log_pruned_stamps_all_rows_sharing_a_basename(self, logs_dir):
        # An archive backing several run_ids → all rows matching its basename.
        for rid in ("r1", "r2"):
            runsdb.begin_run(logs_dir, _row(run_id=rid, log_file="backup.archive.20260920.log"))
        assert runsdb.mark_log_pruned(logs_dir, ["backup.archive.20260920.log"], pruned_by="svc") == 2

    def test_mark_log_pruned_empty_is_noop(self, logs_dir):
        runsdb.begin_run(logs_dir, _row(log_file="backup.x.r1.log"))
        assert runsdb.mark_log_pruned(logs_dir, [], pruned_by="bob") == 0

    def test_repoint_logs_updates_log_file_without_marking_pruned(self, logs_dir):
        runsdb.begin_run(logs_dir, _row(run_id="r1", log_file="backup.x.r1.log"))
        n = runsdb.repoint_logs(logs_dir, {"backup.x.r1.log": "backup.archive.20260925.log"})
        assert n == 1
        r = runsdb.list_runs(logs_dir, live=False)[0]
        assert r["log_file"] == "backup.archive.20260925.log"
        assert r["log_pruned_at"] is None  # a compact move is not a prune

    def test_reindex_preserves_the_prune_stamp(self, logs_dir):
        # A pruned-log row reindexed from a surviving summary keeps its stamp — the
        # log_pruned_* columns are prune-path-owned and excluded from the reindex
        # UPSERT (they're not in _COLUMNS), else the reindex would null them.
        runsdb.begin_run(logs_dir, _row(run_id="r1", runnable="backup", log_file="backup.x.r1.log"))
        runsdb.finish_run(logs_dir, "r1", exit_code=0, duration_ms=5, status="completed")
        runsdb.mark_log_pruned(logs_dir, ["backup.x.r1.log"], pruned_by="bob", pruned_at="2026-09-20T00:00:00+00:00")
        runsdb.reindex(
            logs_dir,
            [{"run_id": "r1", "runnable": "backup", "duration_ms": 5, "exit_code": 0, "ts": "2026-09-25T10:00:00+00:00"}],
        )
        r = runsdb.list_runs(logs_dir, live=False)[0]
        assert r["log_pruned_by"] == "bob"  # survived the reindex UPSERT
        assert r["log_pruned_at"] == "2026-09-20T00:00:00+00:00"

    def test_migration_adds_columns_to_a_v1_db(self, logs_dir):
        # Simulate a pre-0.58 (v1) DB: the runs table without the prune columns.
        # A read-write open (mark_log_pruned) must ALTER them in, then stamp.
        db = runsdb.db_path(logs_dir)
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, runnable TEXT NOT NULL, status TEXT NOT NULL, start_ts TEXT NOT NULL, log_file TEXT)")
        con.execute("INSERT INTO runs (run_id, runnable, status, start_ts, log_file) VALUES ('r1','backup','completed','2026-09-25T10:00:00+00:00','backup.x.r1.log')")
        con.execute("PRAGMA user_version=1")
        con.commit()
        con.close()
        n = runsdb.mark_log_pruned(logs_dir, ["backup.x.r1.log"], pruned_by="bob", pruned_at="2026-09-20T00:00:00+00:00")
        assert n == 1  # migration added the columns, so the UPDATE matched
        r = runsdb.list_runs(logs_dir, live=False)[0]
        assert r["log_pruned_by"] == "bob"

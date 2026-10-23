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
        # "now", not a fixed date: the reaper crashes running rows older than
        # max_run_hours, so a hard-coded timestamp turns into a failure a day later.
        "start_ts": runsdb._now_iso(),
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

    def test_sqlite_without_upsert_keeps_the_start_row(self, logs_dir, monkeypatch):
        # SQLite < 3.24 (CentOS 7's 3.7.17) has no ON CONFLICT … DO UPDATE. The
        # start row (user, args, agent …) must still land and survive the finish,
        # and a re-begin must update it in place without clearing a kept label.
        monkeypatch.setattr(runsdb, "_HAS_UPSERT", False)
        runsdb.begin_run(logs_dir, _row(label="sched:x"))
        r = runsdb.list_runs(logs_dir, live=False)[0]
        assert (r["status"], r["user"], r["label"]) == ("running", "alice", "sched:x")
        runsdb.begin_run(logs_dir, _row(autonomy="manual"))
        runsdb.finish_run(logs_dir, "r1", exit_code=0, duration_ms=7, status="completed")
        rows = runsdb.list_runs(logs_dir, live=False)
        assert len(rows) == 1
        r = rows[0]
        assert (r["status"], r["user"], r["autonomy"], r["label"]) == ("completed", "alice", "manual", "sched:x")
        assert r["args"] == '{"target": "prod"}'

    def test_finish_without_begin_still_records(self, logs_dir):
        # A very short run whose start row lost the race is still recorded whole.
        runsdb.finish_run(logs_dir, "r-late", exit_code=1, duration_ms=5, status="failed")
        rows = {r["run_id"]: r for r in runsdb.list_runs(logs_dir, live=False)}
        assert rows["r-late"]["status"] == "failed"

    def test_finish_without_begin_records_runnable_name(self, logs_dir):
        # The fallback INSERT (no start row — begin_run lost its race or the index
        # was unwritable at start) must carry the runnable name + command; else the
        # console History read (no filename fallback on a DB row) shows "<venv>/"
        # with a blank name.
        runsdb.finish_run(
            logs_dir,
            "r-fb",
            exit_code=0,
            duration_ms=7,
            status="completed",
            runnable="system-info",
            command="disk usage",
        )
        r = {x["run_id"]: x for x in runsdb.list_runs(logs_dir, live=False)}["r-fb"]
        assert r["runnable"] == "system-info"
        assert r["command"] == "disk usage"


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
                    "label": "rundeck",
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
        assert rows["h1"]["trigger"] == "schedule" and rows["h1"]["label"] == "rundeck"
        assert rows["h2"]["label"] is None  # a summary from before labels
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


# ── maintenance introspection: stats / write_error / `runspec history` --json ────


class TestStats:
    def test_missing_db_reports_not_exists(self, logs_dir):
        s = runsdb.stats(logs_dir)
        assert s["exists"] is False
        assert s["rows"] == 0
        assert s["write_error"] == ""

    def test_counts_by_status_and_span(self, logs_dir):
        runsdb.begin_run(logs_dir, _row(run_id="a", start_ts="2026-01-01T00:00:00+00:00", status="completed"))
        runsdb.begin_run(logs_dir, _row(run_id="b", status="failed"))
        runsdb.begin_run(logs_dir, _row(run_id="c"))  # live running (our own pid)
        s = runsdb.stats(logs_dir)
        assert s["exists"] is True
        assert s["rows"] == 3
        assert s["by_status"] == {"completed": 1, "failed": 1, "running": 1}
        assert s["oldest"].startswith("2026-01-01")
        assert s["running"] == 1
        assert s["stale_running"] == 0
        assert s["size_bytes"] > 0
        assert s["schema_version"] == runsdb._SCHEMA_VERSION

    def test_stale_running_counts_dead_pids(self, logs_dir):
        runsdb.begin_run(logs_dir, _row(run_id="dead", pid=2**22 + 12345, proc_start=""))
        assert runsdb.stats(logs_dir)["stale_running"] == 1
        runsdb.reap_now(logs_dir)
        assert runsdb.stats(logs_dir)["stale_running"] == 0

    def test_write_error_names_unwritable_index(self, logs_dir, monkeypatch):
        runsdb.begin_run(logs_dir, _row())
        assert runsdb.write_error(logs_dir) == ""
        # os.access, not chmod: root (CI containers) bypasses permission bits.
        monkeypatch.setattr(runsdb.os, "access", lambda p, mode: False)
        assert "permission denied" in runsdb.write_error(logs_dir)
        assert runsdb.stats(logs_dir)["write_error"]


class TestHistoryCliJson:
    @pytest.fixture
    def cli(self, logs_dir, monkeypatch):
        from runspec import cli
        from runspec import logs as _logs

        monkeypatch.setattr(_logs, "scope_dirs", lambda scope, destructive=False: [logs_dir])
        return cli

    def _json(self, capsys):
        import json

        return json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    def test_stats_json(self, cli, logs_dir, capsys):
        runsdb.begin_run(logs_dir, _row(status="completed"))
        cli.cmd_history(["stats", "--json"])
        out = self._json(capsys)
        assert out["verb"] == "stats"
        assert out["dirs"][0]["rows"] == 1

    def test_prune_json_reports_count(self, cli, logs_dir, capsys):
        for i in range(3):
            runsdb.begin_run(logs_dir, _row(run_id=f"r{i}", status="completed", start_ts=f"2026-01-0{i + 1}T00:00:00+00:00"))
        cli.cmd_history(["prune", "--max-rows", "1", "--json"])
        out = self._json(capsys)
        assert out == {"verb": "prune", "count": 2, "dirs": [{"dir": str(logs_dir), "count": 2}], "ok": True}

    def test_reap_and_reindex_json(self, cli, logs_dir, capsys):
        runsdb.begin_run(logs_dir, _row(run_id="dead", pid=2**22 + 12345, proc_start=""))
        cli.cmd_history(["reap", "--json"])
        assert self._json(capsys)["count"] == 1
        cli.cmd_history(["reindex", "--json"])
        out = self._json(capsys)
        assert out["verb"] == "reindex" and out["ok"] is True

    def test_unwritable_index_fails_named(self, cli, logs_dir, capsys, monkeypatch):
        runsdb.begin_run(logs_dir, _row(status="completed"))
        monkeypatch.setattr(runsdb.os, "access", lambda p, mode: False)
        with pytest.raises(SystemExit):
            cli.cmd_history(["reap", "--json"])
        out = self._json(capsys)
        assert out["ok"] is False
        assert "permission denied" in out["dirs"][0]["error"]

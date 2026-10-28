"""Tests for runspec/logs.py — view / prune / compact of per-invocation logs."""

from __future__ import annotations

import gzip
import io
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from runspec import logs, runsdb

# ── fixtures / builders ───────────────────────────────────────────────────────


def _summary(run_id: str, runnable: str, *, ts: str, user: str = "alice", exit_code: int = 0) -> str:
    return json.dumps(
        {
            "ts": ts,
            "level": "INFO",
            "logger": "runspec.runsummary",
            "message": "run completed",
            "extra": {
                "event": "run_summary",
                "run_id": run_id,
                "runnable": runnable,
                "user": user,
                "exit_code": exit_code,
            },
        }
    )


def _line(run_id: str, message: str, *, ts: str, level: str = "INFO") -> str:
    return json.dumps({"ts": ts, "level": level, "logger": "app", "message": message, "extra": {"run_id": run_id}})


def _id(short: str) -> str:
    """A real run id for a short test id — a per-run file only yields records for
    the run id in its own name, so names and records must agree."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, short))


def _write_run(log_dir: Path, runnable: str, run_id: str, ts_name: str, *lines: str, age_days: float | None = None) -> Path:
    # Filename uses a real uuid token (what _is_managed matches on); the records'
    # extra.run_id is rewritten to match it (see _id).
    real = _id(run_id)
    lines = tuple(ln.replace(f'"run_id": "{run_id}"', f'"run_id": "{real}"') for ln in lines)
    p = log_dir / f"{runnable}.{ts_name}.{real}.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if age_days is not None:
        old = time.time() - age_days * 86400
        os.utime(p, (old, old))
    return p


# ── parsing helpers ───────────────────────────────────────────────────────────


class TestParsers:
    def test_duration(self):
        assert logs.parse_duration("30m") == timedelta(minutes=30)
        assert logs.parse_duration("7d") == timedelta(days=7)
        assert logs.parse_duration("2w") == timedelta(weeks=2)

    def test_duration_invalid(self):
        with pytest.raises(ValueError):
            logs.parse_duration("soon")

    def test_size(self):
        assert logs.parse_size("500KB") == 500 * 1024
        assert logs.parse_size("10MB") == 10 * 1024**2
        assert logs.parse_size("2GB") == 2 * 1024**3


# ── view ───────────────────────────────────────────────────────────────────────


class TestView:
    def test_merges_and_sorts_by_ts(self, tmp_path):
        _write_run(tmp_path, "deploy", "r2", "20260603T150000Z", _summary("r2", "deploy", ts="2026-06-03T15:00:00+00:00"))
        _write_run(tmp_path, "deploy", "r1", "20260601T100000Z", _summary("r1", "deploy", ts="2026-06-01T10:00:00+00:00"))
        recs = logs.collect_records([tmp_path], "deploy")
        ids = [r["extra"]["run_id"] for r, _ in recs]
        assert ids == [_id("r1"), _id("r2")]  # chronological across files

    def test_user_filter_covers_non_summary_lines(self, tmp_path):
        _write_run(
            tmp_path,
            "deploy",
            "r1",
            "20260601T100000Z",
            _line("r1", "hello", ts="2026-06-01T10:00:00+00:00"),
            _summary("r1", "deploy", ts="2026-06-01T10:00:01+00:00", user="bob"),
        )
        _write_run(tmp_path, "deploy", "r2", "20260601T110000Z", _summary("r2", "deploy", ts="2026-06-01T11:00:00+00:00", user="alice"))
        recs = logs.collect_records([tmp_path], "deploy", user="bob")
        # both of bob's run records (the info line + the summary) come through;
        # alice's run is excluded.
        assert {r["extra"]["run_id"] for r, _ in recs} == {_id("r1")}
        assert len(recs) == 2

    def test_run_filter(self, tmp_path):
        _write_run(tmp_path, "deploy", "r1", "20260601T100000Z", _summary("r1", "deploy", ts="2026-06-01T10:00:00+00:00"))
        _write_run(tmp_path, "deploy", "r2", "20260601T110000Z", _summary("r2", "deploy", ts="2026-06-01T11:00:00+00:00"))
        recs = logs.collect_records([tmp_path], "deploy", run=_id("r2"))
        assert [r["extra"]["run_id"] for r, _ in recs] == [_id("r2")]

    def test_run_filter_accepts_the_printed_prefix(self, tmp_path):
        _write_run(tmp_path, "deploy", "r1", "20260601T100000Z", _summary("r1", "deploy", ts="2026-06-01T10:00:00+00:00"))
        _write_run(tmp_path, "deploy", "r2", "20260601T110000Z", _summary("r2", "deploy", ts="2026-06-01T11:00:00+00:00"))
        recs = logs.collect_records([tmp_path], "deploy", run=_id("r2")[:8].upper())
        assert [r["extra"]["run_id"] for r, _ in recs] == [_id("r2")]

    def test_run_filter_unknown_prefix_is_an_error(self, tmp_path):
        _write_run(tmp_path, "deploy", "r1", "20260601T100000Z", _summary("r1", "deploy", ts="2026-06-01T10:00:00+00:00"))
        with pytest.raises(ValueError, match="no run matching 'zzzz' in deploy's logs"):
            logs.collect_records([tmp_path], "deploy", run="zzzz")

    def test_resolve_run_id(self):
        ids = ["3f2a9c1e-aaaa", "3f2a0000-bbbb", "77777777-cccc"]
        assert logs.resolve_run_id("77", ids) == "77777777-cccc"
        assert logs.resolve_run_id("3f2a9", ids) == "3f2a9c1e-aaaa"
        assert logs.resolve_run_id("3f2a0000-bbbb", ids) == "3f2a0000-bbbb"
        with pytest.raises(ValueError, match="matches 2 runs: 3f2a0000-bbbb, 3f2a9c1e-aaaa"):
            logs.resolve_run_id("3f2a", ids)

    def test_runnables_with_logs(self, tmp_path):
        _write_run(tmp_path, "deploy", "r1", "20260601T100000Z", _summary("r1", "deploy", ts="2026-06-01T10:00:00+00:00"))
        _write_run(tmp_path, "backup.web", "r2", "20260601T100000Z", _summary("r2", "backup.web", ts="2026-06-01T10:00:00+00:00"))
        (tmp_path / "single.log").write_text(_line(_id("r3"), "hi", ts="2026-06-01T10:00:00+00:00") + "\n", encoding="utf-8")
        (tmp_path / "audit.jsonl").write_text("", encoding="utf-8")
        # Other scripts' logs (shared ~/logs) aren't runnables: plain text, JSON
        # without a run id, empty.
        (tmp_path / "backup-script.log").write_text("2026-06-01 10:00 starting backup\n", encoding="utf-8")
        (tmp_path / "other.log").write_text(json.dumps({"ts": "x", "level": "INFO", "message": "m"}) + "\n", encoding="utf-8")
        (tmp_path / "empty.log").write_text("", encoding="utf-8")
        assert logs.runnables_with_logs([tmp_path]) == ["backup.web", "deploy", "single"]

    def test_json_passthrough(self, tmp_path):
        raw = _summary(_id("r1"), "deploy", ts="2026-06-01T10:00:00+00:00")
        _write_run(tmp_path, "deploy", "r1", "20260601T100000Z", raw)
        buf = io.StringIO()
        logs.view("deploy", dirs=[tmp_path], as_json=True, out=buf)
        assert buf.getvalue().strip() == raw

    def test_text_line_has_run_and_user_columns(self, tmp_path):
        _write_run(
            tmp_path,
            "deploy",
            "r1",
            "20260601T100000Z",
            _line("r1", "doing", ts="2026-06-01T10:00:00+00:00"),
            _summary("r1", "deploy", ts="2026-06-01T10:00:01+00:00", user="carol"),
        )
        buf = io.StringIO()
        logs.view("deploy", dirs=[tmp_path], out=buf)
        first = buf.getvalue().splitlines()[0]
        assert f"run={_id('r1')[:8]}" in first and "user=carol" in first  # resolved onto the info line


# ── status ─────────────────────────────────────────────────────────────────────


class TestStatus:
    def test_inventory_counts_runs_and_archives(self, tmp_path):
        _write_run(tmp_path, "deploy", "r1", "20260601T100000Z", _summary("r1", "deploy", ts="2026-06-01T10:00:00+00:00"))
        _write_run(tmp_path, "deploy", "r2", "20260602T100000Z", _summary("r2", "deploy", ts="2026-06-02T10:00:00+00:00"))
        _write_run(tmp_path, "scan", "r3", "20260601T100000Z", _summary("r3", "scan", ts="2026-06-01T10:00:00+00:00"))
        inv = logs.inventory([tmp_path])
        assert inv["deploy"]["per_run_files"] == 2
        assert inv["scan"]["per_run_files"] == 1
        assert inv["deploy"]["total_bytes"] > 0

    def test_inventory_ignores_single_mode_file(self, tmp_path):
        single = tmp_path / "deploy.log"
        single.write_text(_summary("r1", "deploy", ts="2026-06-01T10:00:00+00:00") + "\n", encoding="utf-8")
        inv = logs.inventory([tmp_path])
        assert inv == {}  # single-mode active file is never counted

    def test_inventory_counts_archives_separately(self, tmp_path):
        _write_run(tmp_path, "d", "r1", "20260101T100000Z", _summary("r1", "d", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        logs.compact("d", dirs=[tmp_path], older_than=timedelta(days=7), gzip_=True, out=io.StringIO())
        inv = logs.inventory([tmp_path])
        assert inv["d"]["archives"] == 1
        assert inv["d"]["per_run_files"] == 0

    def test_status_json_shape(self, tmp_path):
        _write_run(tmp_path, "deploy", "r1", "20260601T100000Z", _summary("r1", "deploy", ts="2026-06-01T10:00:00+00:00"))
        buf = io.StringIO()
        logs.status(dirs=[tmp_path], as_json=True, out=buf)
        payload = json.loads(buf.getvalue())
        assert payload["total_files"] == 1
        assert payload["runnables"][0]["runnable"] == "deploy"
        assert payload["runnables"][0]["newest"].endswith("Z")
        assert str(tmp_path) in payload["dirs"]

    def test_status_text_empty(self, tmp_path):
        buf = io.StringIO()
        logs.status(dirs=[tmp_path], out=buf)
        assert "No per-invocation logs" in buf.getvalue()


# ── prune ──────────────────────────────────────────────────────────────────────


class TestPrune:
    def test_older_than(self, tmp_path):
        old = _write_run(tmp_path, "d", "r1", "20260101T100000Z", _summary("r1", "d", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        new = _write_run(tmp_path, "d", "r2", "20260603T100000Z", _summary("r2", "d", ts="2026-06-03T10:00:00+00:00"), age_days=1)
        doomed = logs.plan_prune([tmp_path], "d", older_than=timedelta(days=7))
        assert doomed == [old]
        assert new.exists()

    def test_max_files_keeps_newest(self, tmp_path):
        files = [_write_run(tmp_path, "d", f"r{i}", f"2026060{i}T100000Z", _summary(f"r{i}", "d", ts=f"2026-06-0{i}T10:00:00+00:00"), age_days=10 - i) for i in range(1, 5)]
        doomed = set(logs.plan_prune([tmp_path], "d", max_files=2))
        # newest 2 kept (r4, r3 — smaller age), oldest 2 doomed (r1, r2)
        assert doomed == {files[0], files[1]}

    def test_requires_a_policy(self, tmp_path):
        with pytest.raises(ValueError):
            logs.plan_prune([tmp_path], "d")

    def test_never_touches_single_mode_file(self, tmp_path):
        single = tmp_path / "d.log"
        single.write_text(_summary("r1", "d", ts="2026-01-01T00:00:00+00:00") + "\n", encoding="utf-8")
        import os

        old = time.time() - 99 * 86400
        os.utime(single, (old, old))
        doomed = logs.plan_prune([tmp_path], "d", older_than=timedelta(days=7))
        assert single not in doomed
        assert single.exists()

    def test_dry_run_deletes_nothing(self, tmp_path):
        f = _write_run(tmp_path, "d", "r1", "20260101T100000Z", _summary("r1", "d", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        count, _ = logs.prune("d", dirs=[tmp_path], older_than=timedelta(days=7), dry_run=True, out=io.StringIO())
        assert count == 1
        assert f.exists()

    def test_executes_deletion(self, tmp_path):
        f = _write_run(tmp_path, "d", "r1", "20260101T100000Z", _summary("r1", "d", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        count, _ = logs.prune("d", dirs=[tmp_path], older_than=timedelta(days=7), out=io.StringIO())
        assert count == 1
        assert not f.exists()

    def test_json_output_dry_run(self, tmp_path):
        f = _write_run(tmp_path, "d", "r1", "20260101T100000Z", _summary("r1", "d", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        buf = io.StringIO()
        logs.prune("d", dirs=[tmp_path], older_than=timedelta(days=7), dry_run=True, as_json=True, out=buf)
        payload = json.loads(buf.getvalue())
        assert payload["dry_run"] is True
        assert payload["count"] == 1
        assert payload["deleted"][0]["path"] == str(f)
        assert f.exists()  # dry-run preview removed nothing


# ── compact ────────────────────────────────────────────────────────────────────


class TestCompact:
    def test_rolls_old_runs_into_gzip_archive(self, tmp_path):
        old = _write_run(tmp_path, "d", "r1", "20260101T100000Z", _summary("r1", "d", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        recent = _write_run(tmp_path, "d", "r2", "20260603T100000Z", _summary("r2", "d", ts="2026-06-03T10:00:00+00:00"), age_days=1)
        n = logs.compact("d", dirs=[tmp_path], older_than=timedelta(days=7), gzip_=True, out=io.StringIO())
        assert n == 1
        assert not old.exists()  # original removed
        assert recent.exists()  # too new to compact
        archives = list(tmp_path.glob("d.archive.*.log.gz"))
        assert len(archives) == 1
        with gzip.open(archives[0], "rt", encoding="utf-8") as fh:
            assert _id("r1") in fh.read()

    def test_archive_is_console_discoverable(self, tmp_path):
        _write_run(tmp_path, "d", "r1", "20260101T100000Z", _summary("r1", "d", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        logs.compact("d", dirs=[tmp_path], older_than=timedelta(days=7), gzip_=True, out=io.StringIO())
        # the archive name matches the runnable-scoped discovery glob
        assert list(tmp_path.glob("d.*.log.gz"))

    def test_view_reads_compacted_archive(self, tmp_path):
        _write_run(tmp_path, "d", "r1", "20260101T100000Z", _summary("r1", "d", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        logs.compact("d", dirs=[tmp_path], older_than=timedelta(days=7), gzip_=True, out=io.StringIO())
        recs = logs.collect_records([tmp_path], "d")
        assert [r["extra"]["run_id"] for r, _ in recs] == [_id("r1")]

    def test_dry_run_keeps_originals(self, tmp_path):
        old = _write_run(tmp_path, "d", "r1", "20260101T100000Z", _summary("r1", "d", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        logs.compact("d", dirs=[tmp_path], older_than=timedelta(days=7), dry_run=True, out=io.StringIO())
        assert old.exists()
        assert not list(tmp_path.glob("d.archive.*"))

    def test_does_not_compact_existing_archive(self, tmp_path):
        # an archive itself must not be re-compacted
        _write_run(tmp_path, "d", "r1", "20260101T100000Z", _summary("r1", "d", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        logs.compact("d", dirs=[tmp_path], older_than=timedelta(days=7), gzip_=True, out=io.StringIO())
        n = logs.compact("d", dirs=[tmp_path], older_than=timedelta(days=7), gzip_=True, out=io.StringIO())
        assert n == 0  # nothing left to compact; the archive is skipped

    def test_json_output(self, tmp_path):
        _write_run(tmp_path, "d", "r1", "20260101T100000Z", _summary("r1", "d", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        buf = io.StringIO()
        logs.compact("d", dirs=[tmp_path], older_than=timedelta(days=7), gzip_=True, as_json=True, out=buf)
        payload = json.loads(buf.getvalue())
        assert payload["compacted"] == 1
        assert payload["archives"][0]["archive"].endswith(".log.gz")


# ── classification + scope (shared with the console; the venv-scoping fix) ─────


class TestClassify:
    def test_classify_name(self):
        assert logs.classify_name("deploy.20260101T100000Z.abcd1234.log") == "per_run"
        assert logs.classify_name("deploy.archive.20260101.log") == "archive"
        assert logs.classify_name("deploy.archive.20260101.log.gz") == "archive"
        assert logs.classify_name("deploy.log") == "single"
        # a runnable whose own name contains a dot is not mistaken for a per-run file
        assert logs.classify_name("deploy.web.log") == "single"

    def test_runnable_of(self):
        assert logs.runnable_of("deploy.20260101T100000Z.abcd1234.log") == "deploy"
        assert logs.runnable_of("deploy.archive.20260101.log.gz") == "deploy"
        assert logs.runnable_of("deploy.log") == "deploy"
        assert logs.runnable_of("deploy.web.log") == "deploy.web"


class TestScopeDirs:
    def test_explicit_scopes(self, monkeypatch, tmp_path):
        venv, home = tmp_path / "venv", tmp_path / "home"
        monkeypatch.setattr(logs.sys, "prefix", str(venv))
        monkeypatch.setattr(logs.Path, "home", classmethod(lambda cls: home))
        assert logs.scope_dirs("venv", destructive=True) == [venv / "logs"]
        assert logs.scope_dirs("home", destructive=True) == [home / "logs"]
        assert logs.scope_dirs("all", destructive=True) is None

    def test_default_destructive_is_venv_only(self, monkeypatch, tmp_path):
        # The data-loss fix: a bare prune/compact defaults to this venv's own dir,
        # never the shared ~/logs.
        monkeypatch.setattr(logs.sys, "prefix", str(tmp_path / "venv"))
        assert logs.scope_dirs(None, destructive=True) == [tmp_path / "venv" / "logs"]

    def test_default_read_is_union(self):
        # Reads (status/view) stay informative — the log_dirs() union.
        assert logs.scope_dirs(None, destructive=False) is None

    def test_default_prune_leaves_shared_home_untouched(self, monkeypatch, tmp_path):
        # End-to-end regression: two venvs share ~/logs; a default-scope prune on
        # venv A must not delete venv B's files that live in the shared home dir.
        venv_a, home = tmp_path / "venvA", tmp_path / "home"
        (venv_a / "logs").mkdir(parents=True)
        (home / "logs").mkdir(parents=True)
        monkeypatch.setattr(logs.sys, "prefix", str(venv_a))
        monkeypatch.setattr(logs.Path, "home", classmethod(lambda cls: home))
        own = _write_run(venv_a / "logs", "a", "r1", "20260101T100000Z", _summary("r1", "a", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        shared = _write_run(home / "logs", "b", "r2", "20260101T100000Z", _summary("r2", "b", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        logs.prune(None, dirs=logs.scope_dirs(None, destructive=True), older_than=timedelta(days=7), out=io.StringIO())
        assert not own.exists()  # this venv's old file is pruned
        assert shared.exists()  # the shared ~/logs file (another venv's) is untouched


class TestPruneProvenanceIntegration:
    """§8.1 end-to-end: prune stamps the runs index (log_pruned_at/by) for the row
    whose file it deleted; compact repoints the row instead (content survives)."""

    def _index_row(self, d: Path, run_id: str, runnable: str, log_file: str) -> None:
        # A recent start_ts (within the DB's 90-day ring) so the row survives
        # finish_run's maintenance and outlives its log file — the whole point of
        # the stamp. The log FILE ages independently (age_days below) to be prunable.
        start_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        runsdb.begin_run(
            d,
            {
                "run_id": run_id,
                "runnable": runnable,
                "status": "running",
                "start_ts": start_ts,
                "log_file": log_file,
                "pid": 0,
                "host": "h",
                "proc_start": "",
            },
        )
        runsdb.finish_run(d, run_id, exit_code=0, duration_ms=1, status="completed")

    def test_prune_stamps_the_runs_index(self, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        p = _write_run(d, "backup", "r1", "20260101T000000Z", _summary("r1", "backup", ts="2026-01-01T00:00:00+00:00"), age_days=30)
        self._index_row(d, "r1", "backup", p.name)
        logs.prune("backup", dirs=[d], older_than=timedelta(days=7), out=io.StringIO())
        assert not p.exists()  # the log file was pruned…
        r = runsdb.list_runs(d, live=False)[0]
        assert r["log_pruned_at"]  # …and the surviving row records when…
        assert r["log_pruned_by"]  # …and by whom (the OS user running the prune)

    def test_dry_run_prune_leaves_the_stamp_unset(self, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        p = _write_run(d, "backup", "r1", "20260101T000000Z", _summary("r1", "backup", ts="2026-01-01T00:00:00+00:00"), age_days=30)
        self._index_row(d, "r1", "backup", p.name)
        logs.prune("backup", dirs=[d], older_than=timedelta(days=7), dry_run=True, out=io.StringIO())
        assert p.exists()  # nothing deleted…
        assert runsdb.list_runs(d, live=False)[0]["log_pruned_at"] is None  # …so no stamp

    def test_compact_repoints_the_runs_index(self, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        p = _write_run(d, "backup", "r1", "20260101T000000Z", _summary("r1", "backup", ts="2026-01-01T00:00:00+00:00"), age_days=30)
        self._index_row(d, "r1", "backup", p.name)
        logs.compact("backup", dirs=[d], older_than=timedelta(days=7), out=io.StringIO())
        assert not p.exists()  # folded into the archive
        archive = d / f"backup.archive.{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"
        assert archive.exists()
        r = runsdb.list_runs(d, live=False)[0]
        assert r["log_file"] == archive.name  # repointed to the archive…
        assert r["log_pruned_at"] is None  # …not marked pruned (the content survives)


class TestShare:
    """`runspec logs share`: make a logs dir group-writable (POSIX) so several
    accounts can write the same runs index + audit logs."""

    def test_chmods_dir_to_2775_setgid(self, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        logs.share([d], out=io.StringIO())
        # group-writable + setgid (so files created here inherit the dir's group)
        assert d.stat().st_mode & 0o7777 == 0o2775

    def test_creates_dir_if_missing(self, tmp_path):
        d = tmp_path / "venv" / "logs"  # neither the dir nor its parent exists yet
        logs.share([d], out=io.StringIO())
        assert d.is_dir()
        assert d.stat().st_mode & 0o7777 == 0o2775

    def test_repairs_existing_runs_index_perms(self, tmp_path):
        # A dir chmod never touches a file already inside it, so an index another
        # account created with restrictive perms is repaired to group-writable.
        d = tmp_path / "logs"
        d.mkdir()
        db = runsdb.db_path(d)
        db.write_bytes(b"")
        os.chmod(db, 0o600)
        results = logs.share([d], out=io.StringIO())
        assert db.stat().st_mode & 0o777 == 0o664
        assert results[0]["db_repaired"] is True

    def test_no_index_no_repair(self, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        results = logs.share([d], out=io.StringIO())
        assert results[0]["db_repaired"] is False

    def test_chgrp_dir_and_index_when_group_given(self, tmp_path, monkeypatch):
        # chgrp needs a real group + privilege, so record the calls instead.
        d = tmp_path / "logs"
        d.mkdir()
        db = runsdb.db_path(d)
        db.write_bytes(b"")
        calls: list[tuple[Path, str | None]] = []
        monkeypatch.setattr(logs.shutil, "chown", lambda p, group=None: calls.append((Path(p), group)))
        logs.share([d], group="consoles", out=io.StringIO())
        assert (d, "consoles") in calls  # the dir…
        assert (db, "consoles") in calls  # …and the runs index inside it

    def test_no_chgrp_when_group_omitted(self, tmp_path, monkeypatch):
        d = tmp_path / "logs"
        d.mkdir()
        calls: list[object] = []
        monkeypatch.setattr(logs.shutil, "chown", lambda *a, **k: calls.append((a, k)))
        logs.share([d], out=io.StringIO())
        assert calls == []  # group left unchanged when --group is not given

    def test_json_output_shape(self, tmp_path, monkeypatch):
        # Monkeypatch chown: a real chgrp needs the group to exist on the box (it
        # won't on a CI runner), and here we're asserting the JSON shape — that the
        # group arg is echoed into the payload — not the chown itself.
        monkeypatch.setattr(logs.shutil, "chown", lambda *a, **k: None)
        d = tmp_path / "logs"
        d.mkdir()
        buf = io.StringIO()
        logs.share([d], group="consoles", as_json=True, out=buf)
        payload = json.loads(buf.getvalue())
        assert payload["shared"][0]["dir"] == str(d)
        assert payload["shared"][0]["group"] == "consoles"
        assert payload["shared"][0]["mode"] == "2775"

    def test_posix_gate_refuses_on_non_posix(self, tmp_path, monkeypatch):
        # No os.getuid ⇒ a non-POSIX host ⇒ refuse rather than pretend to share.
        monkeypatch.delattr(logs.os, "getuid", raising=False)
        with pytest.raises(ValueError, match="POSIX"):
            logs.share([tmp_path / "logs"])

    def test_scope_default_is_this_venv(self, tmp_path, monkeypatch):
        # `runspec logs share` with no --scope shares this venv's own dir only.
        venv = tmp_path / "venv"
        monkeypatch.setattr(logs.sys, "prefix", str(venv))
        results = logs.share(dirs=logs.scope_dirs(None, destructive=True), out=io.StringIO())
        assert results[0]["dir"] == str(venv / "logs")
        assert (venv / "logs").is_dir()

    def test_cli_share_verb_dispatches(self, monkeypatch):
        # The `share` verb + `--group`/`--json` args parse and reach logs.share.
        from runspec import cli

        seen: dict[str, object] = {}
        monkeypatch.setattr(
            logs,
            "share",
            lambda *, dirs, group, acl_users, as_json, **_: seen.update(group=group, as_json=as_json),
        )
        cli.cmd_logs(["share", "--group", "consoles", "--json"])
        assert seen == {"group": "consoles", "as_json": True}

    def test_cli_share_permission_error_is_clean(self, monkeypatch, capsys):
        # A chmod the caller can't make surfaces as a clean ✗ line + exit 1, not a
        # traceback (the console reads that stdout to decide "set Run as").
        from runspec import cli

        def boom(**_kwargs):
            raise PermissionError("[Errno 1] Operation not permitted: '/opt/venv/logs'")

        monkeypatch.setattr(logs, "share", boom)
        with pytest.raises(SystemExit) as exc:
            cli.cmd_logs(["share", "--group", "consoles"])
        assert exc.value.code == 1
        assert "✗" in capsys.readouterr().out

    def test_acl_grants_via_setfacl(self, tmp_path, monkeypatch):
        # --acl-user runs setfacl (access + default ACL) granting the user rwX, and
        # records "applied". Monkeypatch setfacl so it needs no real ACL support.
        d = tmp_path / "logs"
        d.mkdir()
        calls: list[list[str]] = []

        def fake_run(argv, **_kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(logs.shutil, "which", lambda _name: "/usr/bin/setfacl")
        monkeypatch.setattr(logs.subprocess, "run", fake_run)
        results = logs.share([d], acl_users=["alice"], out=io.StringIO())
        assert results[0]["acl"] == "applied"
        assert results[0]["acl_users"] == ["alice"]
        # An access pass (-R -m) and a default pass (-R -d -m), both granting rwX.
        assert calls[0][:3] == ["/usr/bin/setfacl", "-R", "-m"]
        assert "u:alice:rwX" in calls[0][3]
        assert any(a[1:4] == ["-R", "-d", "-m"] for a in calls)

    def test_acl_multiple_users_in_one_spec(self, tmp_path, monkeypatch):
        d = tmp_path / "logs"
        d.mkdir()
        calls: list[list[str]] = []
        monkeypatch.setattr(logs.shutil, "which", lambda _name: "/usr/bin/setfacl")
        monkeypatch.setattr(logs.subprocess, "run", lambda argv, **_k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
        logs.share([d], acl_users=["alice", "svc"], out=io.StringIO())
        assert calls[0][3] == "u:alice:rwX,u:svc:rwX"

    def test_acl_unsupported_when_no_setfacl(self, tmp_path, monkeypatch):
        d = tmp_path / "logs"
        d.mkdir()
        monkeypatch.setattr(logs.shutil, "which", lambda _name: None)
        results = logs.share([d], acl_users=["alice"], out=io.StringIO())
        assert results[0]["acl"].startswith("unsupported")

    def test_acl_unsupported_when_filesystem_rejects(self, tmp_path, monkeypatch):
        d = tmp_path / "logs"
        d.mkdir()
        monkeypatch.setattr(logs.shutil, "which", lambda _name: "/usr/bin/setfacl")
        monkeypatch.setattr(
            logs.subprocess,
            "run",
            lambda argv, **_k: subprocess.CompletedProcess(argv, 1, "", "setfacl: logs: Operation not supported"),
        )
        results = logs.share([d], acl_users=["alice"], out=io.StringIO())
        assert results[0]["acl"] == "unsupported: filesystem has no ACL support"

    def test_acl_failure_does_not_fail_the_share(self, tmp_path, monkeypatch):
        # A setfacl error still leaves the group share applied (2775) — ACL is a bonus.
        d = tmp_path / "logs"
        d.mkdir()
        monkeypatch.setattr(logs.shutil, "which", lambda _name: "/usr/bin/setfacl")
        monkeypatch.setattr(
            logs.subprocess,
            "run",
            lambda argv, **_k: subprocess.CompletedProcess(argv, 1, "", "setfacl: some other error"),
        )
        results = logs.share([d], acl_users=["alice"], out=io.StringIO())
        assert results[0]["acl"].startswith("error:")
        assert d.stat().st_mode & 0o7777 == 0o2775  # the share itself still applied

    def test_no_acl_key_without_users(self, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        results = logs.share([d], out=io.StringIO())
        assert "acl" not in results[0]

    def test_cli_share_acl_user_dispatches(self, monkeypatch):
        from runspec import cli

        seen: dict[str, object] = {}
        monkeypatch.setattr(
            logs,
            "share",
            lambda *, dirs, group, acl_users, as_json, **_: seen.update(acl_users=acl_users),
        )
        cli.cmd_logs(["share", "--acl-user", "alice", "--acl-user", "svc"])
        assert seen["acl_users"] == ["alice", "svc"]

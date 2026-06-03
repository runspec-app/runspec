"""Tests for runspec/logs.py — view / prune / compact of per-invocation logs."""

from __future__ import annotations

import gzip
import io
import json
import os
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from runspec import logs

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


def _write_run(log_dir: Path, runnable: str, run_id: str, ts_name: str, *lines: str, age_days: float | None = None) -> Path:
    # Filename uses a real uuid token (what _is_managed matches on); the record's
    # extra.run_id stays the short test value the assertions read.
    p = log_dir / f"{runnable}.{ts_name}.{uuid.uuid4()}.log"
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
        assert ids == ["r1", "r2"]  # chronological across files

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
        assert {r["extra"]["run_id"] for r, _ in recs} == {"r1"}
        assert len(recs) == 2

    def test_run_filter(self, tmp_path):
        _write_run(tmp_path, "deploy", "r1", "20260601T100000Z", _summary("r1", "deploy", ts="2026-06-01T10:00:00+00:00"))
        _write_run(tmp_path, "deploy", "r2", "20260601T110000Z", _summary("r2", "deploy", ts="2026-06-01T11:00:00+00:00"))
        recs = logs.collect_records([tmp_path], "deploy", run="r2")
        assert [r["extra"]["run_id"] for r, _ in recs] == ["r2"]

    def test_json_passthrough(self, tmp_path):
        raw = _summary("r1", "deploy", ts="2026-06-01T10:00:00+00:00")
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
        assert "run=r1" in first and "user=carol" in first  # resolved onto the info line


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
            assert "r1" in fh.read()

    def test_archive_is_console_discoverable(self, tmp_path):
        _write_run(tmp_path, "d", "r1", "20260101T100000Z", _summary("r1", "d", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        logs.compact("d", dirs=[tmp_path], older_than=timedelta(days=7), gzip_=True, out=io.StringIO())
        # the archive name matches the runnable-scoped discovery glob
        assert list(tmp_path.glob("d.*.log.gz"))

    def test_view_reads_compacted_archive(self, tmp_path):
        _write_run(tmp_path, "d", "r1", "20260101T100000Z", _summary("r1", "d", ts="2026-01-01T10:00:00+00:00"), age_days=30)
        logs.compact("d", dirs=[tmp_path], older_than=timedelta(days=7), gzip_=True, out=io.StringIO())
        recs = logs.collect_records([tmp_path], "d")
        assert [r["extra"]["run_id"] for r, _ in recs] == ["r1"]

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

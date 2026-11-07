"""Tests for runspec/retention.py — a venv's own log retention policy."""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from runspec import logs, retention, runsdb


def _per_run(log_dir: Path, runnable: str, *, age_days: float) -> Path:
    rid = str(uuid.uuid4())
    p = log_dir / f"{runnable}.20260101T100000Z.{rid}.log"
    p.write_text(json.dumps({"ts": "2026-01-01T10:00:00+00:00", "level": "INFO", "message": "x", "extra": {"run_id": rid}}) + "\n", encoding="utf-8")
    old = time.time() - age_days * 86400
    os.utime(p, (old, old))
    return p


@pytest.fixture
def venv(tmp_path, monkeypatch):
    root = tmp_path / "venv"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(root))
    return root


# ── the policy file ───────────────────────────────────────────────────────────


class TestPolicy:
    def test_round_trip(self, venv):
        p = retention.Policy(compact_after="7d", delete_after="90d")
        path = retention.write_policy(p)
        assert path == venv / retention.POLICY_FILE
        assert retention.read_policy() == p
        assert oct(path.stat().st_mode & 0o777) == "0o644"

    def test_absent_is_none(self, venv):
        assert retention.read_policy() is None

    @pytest.mark.parametrize(
        "body, match",
        [
            ("[logs]\n", "at least one"),
            ('[logs]\ncompact_after = "soon"\n', "invalid duration"),
            ('[logs]\nmax_total_size = "lots"\n', "invalid size"),
            ('[logs]\ncompact_after = "90d"\ndelete_after = "7d"\n', "shorter than"),
            ('[logs]\nkeep = "7d"\n', "unknown key"),
            ("[logs]\ndelete_after = 90\n", "must be a string"),
            ('compact_after = "7d"\n', r"\[logs\] table"),
            ("[logs\n", "venv"),
        ],
    )
    def test_invalid(self, venv, body, match):
        (venv / retention.POLICY_FILE).write_text(body, encoding="utf-8")
        with pytest.raises(ValueError, match=match):
            retention.read_policy()

    def test_update_keeps_clears_and_sets(self):
        cur = retention.Policy(compact_after="7d", delete_after="90d")
        new = retention.update_policy(cur, compact_after="off", delete_after=None, max_total_size="5GB")
        assert new == retention.Policy(compact_after="", delete_after="90d", max_total_size="5GB")
        assert retention.update_policy(None, delete_after="30d") == retention.Policy(delete_after="30d")

    @pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership")
    def test_group_writable_policy_is_not_trusted(self, venv):
        retention.write_policy(retention.Policy(delete_after="90d"))
        assert retention.trusted()
        os.chmod(venv / retention.POLICY_FILE, 0o664)
        assert not retention.trusted()


# ── applying it ───────────────────────────────────────────────────────────────


class TestApply:
    def test_compacts_then_prunes(self, venv):
        d = venv / "logs"
        fresh = _per_run(d, "job", age_days=1)
        mid = _per_run(d, "job", age_days=10)
        old = _per_run(d, "job", age_days=120)
        single = d / "job.log"
        single.write_text("{}\n", encoding="utf-8")
        os.utime(single, (time.time() - 200 * 86400,) * 2)
        audit = d / "audit.jsonl"
        audit.write_text("{}\n", encoding="utf-8")
        os.utime(audit, (time.time() - 200 * 86400,) * 2)

        res = retention.apply(retention.Policy(compact_after="7d", delete_after="90d"))
        # The 120-day run is deleted before compaction (never folded into an
        # archive that would outlive it); the 10-day run is compressed.
        assert res["compacted"] == 1 and res["deleted"] == 1 and "error" not in res
        assert fresh.exists() and not mid.exists() and not old.exists()
        # Never touched: the single-mode file and the audit ledger.
        assert single.exists() and audit.exists()
        archives = list(d.glob("job.archive.*.log.gz"))
        assert len(archives) == 1
        # The archive's mtime is its newest run (10 days), not "now".
        assert time.time() - archives[0].stat().st_mtime == pytest.approx(10 * 86400, abs=60)
        with gzip.open(archives[0], "rt") as fh:
            assert len(fh.read().splitlines()) == 1

    def test_archive_ages_out_with_its_runs(self, venv):
        d = venv / "logs"
        _per_run(d, "job", age_days=20)
        _per_run(d, "job", age_days=40)
        policy = retention.Policy(compact_after="7d", delete_after="30d")
        assert retention.apply(policy)["compacted"] == 1
        archives = list(d.glob("job.archive.*.log.gz"))
        assert len(archives) == 1
        # Fifteen days later the archived run is 35 days old → the archive goes.
        later = time.time() - 35 * 86400
        os.utime(archives[0], (later, later))
        assert retention.apply(policy)["deleted"] == 1
        assert not list(d.glob("job.*"))

    def test_runs_from_different_days_get_different_archives(self, venv):
        d = venv / "logs"
        _per_run(d, "job", age_days=10)
        _per_run(d, "job", age_days=12)
        retention.apply(retention.Policy(compact_after="7d"))
        assert len(list(d.glob("job.archive.*.log.gz"))) == 2

    def test_dry_run_changes_nothing(self, venv):
        d = venv / "logs"
        a = _per_run(d, "job", age_days=10)
        b = _per_run(d, "job", age_days=120)
        res = retention.apply(retention.Policy(compact_after="7d", delete_after="90d"), dry_run=True)
        assert res["dry_run"] and res["compacted"] == 2
        assert a.exists() and b.exists()

    def test_max_total_size(self, venv):
        d = venv / "logs"
        files = [_per_run(d, "job", age_days=n) for n in (1, 2, 3)]
        size = files[0].stat().st_size
        res = retention.apply(retention.Policy(max_total_size=f"{size * 2}B"))
        assert res["deleted"] == 1 and not files[2].exists()


class TestRunPass:
    def test_records_last_and_throttles(self, venv):
        retention.write_policy(retention.Policy(delete_after="30d"))
        _per_run(venv / "logs", "job", age_days=40)
        first = retention.run_pass()
        assert first["deleted"] == 1
        assert retention.read_last()["deleted"] == 1
        assert retention.run_pass() == {"skipped": "not-due"}
        _per_run(venv / "logs", "job", age_days=40)
        assert retention.run_pass(force=True)["deleted"] == 1

    def test_due_after_a_day(self, venv):
        retention.write_policy(retention.Policy(delete_after="30d"))
        retention.run_pass()
        last = venv / "logs" / retention.LAST_FILE
        assert not retention.due()
        os.utime(last, (time.time() - retention.INTERVAL_S - 5,) * 2)
        assert retention.due()

    def test_no_policy_no_logs_invalid(self, venv, tmp_path):
        assert retention.run_pass() == {"skipped": "no-policy"}
        (venv / retention.POLICY_FILE).write_text("[logs]\n", encoding="utf-8")
        assert retention.run_pass()["skipped"] == "invalid"
        other = tmp_path / "bare"
        other.mkdir()
        retention.write_policy(retention.Policy(delete_after="30d"), other)
        assert retention.run_pass(other) == {"skipped": "no-logs"}

    def test_dry_run_records_nothing(self, venv):
        retention.write_policy(retention.Policy(delete_after="30d"))
        retention.run_pass(force=True, dry_run=True)
        assert retention.read_last() is None

    @pytest.mark.skipif(sys.platform == "win32", reason="flock")
    def test_busy_when_locked(self, venv):
        import fcntl

        retention.write_policy(retention.Policy(delete_after="30d"))
        fd = os.open(venv / "logs" / retention.LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            assert retention.run_pass(force=True) == {"skipped": "busy"}
        finally:
            os.close(fd)


# ── the end-of-run hook ───────────────────────────────────────────────────────


class TestMaybeApply:
    def test_spawns_only_when_everything_lines_up(self, venv, monkeypatch):
        spawned: list[Path] = []
        monkeypatch.setattr(retention, "_spawn", lambda root: spawned.append(root))
        d = venv / "logs"
        assert not retention.maybe_apply(d)  # no policy
        retention.write_policy(retention.Policy(delete_after="30d"))
        assert retention.maybe_apply(d)
        assert spawned == [venv]
        retention.run_pass()  # a pass just ran → not due
        assert not retention.maybe_apply(d)
        assert spawned == [venv]

    def test_never_for_another_logs_dir(self, venv, tmp_path, monkeypatch):
        monkeypatch.setattr(retention, "_spawn", lambda root: pytest.fail("spawned"))
        home = tmp_path / "home"
        (home / "logs").mkdir(parents=True)
        retention.write_policy(retention.Policy(delete_after="30d"), home)
        assert not retention.maybe_apply(home / "logs")

    @pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership")
    def test_never_for_a_non_owner(self, venv, monkeypatch):
        monkeypatch.setattr(retention, "_spawn", lambda root: pytest.fail("spawned"))
        retention.write_policy(retention.Policy(delete_after="30d"))
        monkeypatch.setattr(retention.os, "geteuid", lambda: os.stat(venv).st_uid + 1)
        assert not retention.maybe_apply(venv / "logs")

    def test_finish_run_calls_the_hook(self, venv, monkeypatch):
        seen: list[Path] = []
        monkeypatch.setattr(retention, "maybe_apply", lambda d: seen.append(Path(d)))
        d = venv / "logs"
        runsdb.finish_run(d, str(uuid.uuid4()), exit_code=0, duration_ms=1, status="completed", runnable="job")
        assert seen == [d]

    def test_detached_pass_runs_for_real(self, venv):
        # The real `python -m runspec.retention <root>` the hook starts.
        retention.write_policy(retention.Policy(delete_after="30d"))
        old = _per_run(venv / "logs", "job", age_days=40)
        env = {**os.environ, "PYTHONPATH": str(Path(retention.__file__).parent.parent)}
        subprocess.run([sys.executable, "-m", "runspec.retention", str(venv)], env=env, check=True, timeout=60)
        assert not old.exists()
        assert retention.read_last()["deleted"] == 1


# ── the CLI verb ──────────────────────────────────────────────────────────────


class TestCli:
    def test_set_show_now_off(self, venv, capsys):
        from runspec import cli

        cli.cmd_logs(["retention", "--compact-after", "7d", "--delete-after", "90d"])
        assert retention.read_policy() == retention.Policy(compact_after="7d", delete_after="90d")
        capsys.readouterr()

        cli.cmd_logs(["retention", "--json"])
        info = json.loads(capsys.readouterr().out)
        assert info["policy"] == {"compact_after": "7d", "delete_after": "90d"}

        cli.cmd_logs(["retention", "--compact-after", "off"])
        assert retention.read_policy() == retention.Policy(delete_after="90d")

        old = _per_run(venv / "logs", "job", age_days=100)
        cli.cmd_logs(["retention", "--now", "--dry-run"])
        assert "would delete 1" in capsys.readouterr().out and old.exists()
        cli.cmd_logs(["retention", "--now", "--json"])
        assert json.loads(capsys.readouterr().out)["pass"]["deleted"] == 1
        assert not old.exists()

        cli.cmd_logs(["retention", "--off"])
        assert retention.read_policy() is None
        assert "removed" in capsys.readouterr().out

    def test_bad_value_is_a_clean_error(self, venv, capsys):
        from runspec import cli

        with pytest.raises(SystemExit) as exc:
            cli.cmd_logs(["retention", "--compact-after", "90d", "--delete-after", "7d"])
        assert exc.value.code == 1
        assert "shorter than" in capsys.readouterr().out
        assert retention.read_policy() is None

    def test_now_without_policy_fails(self, venv, capsys):
        from runspec import cli

        with pytest.raises(SystemExit):
            cli.cmd_logs(["retention", "--now"])
        assert "no retention policy" in capsys.readouterr().out


def test_logs_status_ignores_the_bookkeeping_files(venv):
    # .retention-last.json / .retention.lock aren't log files.
    retention.write_policy(retention.Policy(delete_after="30d"))
    retention.run_pass()
    assert all(not f.name.startswith(".retention") for f in logs._discover([venv / "logs"], None))

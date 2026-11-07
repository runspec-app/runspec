"""Who is handling a failing job: runspec.runnotes + the run_notes table + the
`runspec history claim/resolve/release/notes/jobs` verbs."""

from __future__ import annotations

import json
import os

import pytest

from runspec import runnotes, runsdb


@pytest.fixture
def logs_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    os.chmod(d, 0o2775)
    return d


def _run(run_id, status, ts, label="sched:nightly", runnable="backup"):
    return {
        "run_id": run_id,
        "runnable": runnable,
        "command": "",
        "status": status,
        "start_ts": ts,
        "end_ts": ts,
        "user": "svc",
        "agent": False,
        "trigger": "schedule:cron",
        "label": label,
        "exit_code": 0 if status == "completed" else 1,
    }


def _ts(day, hour=0):
    return f"2026-11-{day:02d}T{hour:02d}:00:00+00:00"


# ── pure ────────────────────────────────────────────────────────────────────────


class TestJobState:
    def test_clean_when_newest_finished_succeeded(self):
        rows = [_run("c", "running", _ts(3)), _run("b", "completed", _ts(2)), _run("a", "failed", _ts(1))]
        assert runnotes.job_state(rows)["state"] == "clean"

    def test_streak_counts_failures_since_last_success(self):
        rows = [_run("d", "failed", _ts(4)), _run("c", "skipped", _ts(3)), _run("b", "crashed", _ts(2)), _run("a", "completed", _ts(1))]
        st = runnotes.job_state(rows)
        assert st == {"state": "failed", "failures": 2, "since": _ts(2), "run_id": "d", "note": None}

    def test_claim_on_older_failure_covers_the_streak(self):
        note = {"state": "handling", "ts": _ts(3, 1), "actor": "jason"}
        rows = [_run("c", "failed", _ts(4)), _run("b", "failed", _ts(3)) | {"note": note}, _run("a", "completed", _ts(1))]
        st = runnotes.job_state(rows)
        assert st["state"] == "handling" and st["note"]["actor"] == "jason" and st["run_id"] == "c"

    def test_note_before_last_success_no_longer_applies(self):
        note = {"state": "done", "ts": _ts(1, 1), "actor": "jason"}
        rows = [_run("c", "failed", _ts(4)), _run("b", "completed", _ts(2)), _run("a", "failed", _ts(1)) | {"note": note}]
        assert runnotes.job_state(rows)["state"] == "failed"

    def test_newest_note_wins_and_release_reopens(self):
        rows = [
            _run("b", "failed", _ts(3)) | {"note": {"state": "released", "ts": _ts(3, 2)}},
            _run("a", "failed", _ts(2)) | {"note": {"state": "done", "ts": _ts(2, 1)}},
        ]
        assert runnotes.job_state(rows)["state"] == "failed"

    def test_jobs_groups_by_label_then_runnable(self):
        rows = [_run("a", "failed", _ts(1)), _run("b", "completed", _ts(2), label="", runnable="report")]
        assert {k: v["state"] for k, v in runnotes.jobs(rows).items()} == {"sched:nightly": "failed", "report": "clean"}

    def test_clean_note_is_one_bounded_line(self):
        assert runnotes.clean_note("a\n  b") == "a b"
        assert len(runnotes.clean_note("x" * 900)) == runnotes.MAX_NOTE


# ── the table ─────────────────────────────────────────────────────────────────


class TestNotesTable:
    def test_add_note_attaches_newest_to_list_rows(self, logs_dir):
        runsdb.begin_run(logs_dir, _run("r1aaaaaa", "failed", _ts(1)))
        run = runsdb.find_run(logs_dir, "r1aa")
        runsdb.add_note(logs_dir, run, "handling", "looking", actor="jason", os_user="svc")
        runsdb.add_note(logs_dir, run, "done", "disk full", actor="ana", os_user="svc")
        rows = runsdb.list_runs(logs_dir, live=False)
        assert rows[0]["note"]["state"] == "done" and rows[0]["note"]["actor"] == "ana"
        assert [n["state"] for n in runsdb.list_notes(logs_dir)] == ["done", "handling"]

    def test_rows_without_notes_carry_none(self, logs_dir):
        runsdb.begin_run(logs_dir, _run("r1", "failed", _ts(1)))
        assert runsdb.list_runs(logs_dir, live=False)[0]["note"] is None

    def test_ambiguous_prefix_is_refused(self, logs_dir):
        runsdb.begin_run(logs_dir, _run("abc1", "failed", _ts(1)))
        runsdb.begin_run(logs_dir, _run("abc2", "failed", _ts(2)))
        with pytest.raises(runsdb.NoteError):
            runsdb.find_run(logs_dir, "abc")
        assert runsdb.find_run(logs_dir, "zzz") is None

    def test_reindex_upsert_keeps_notes(self, logs_dir):
        runsdb.begin_run(logs_dir, _run("r1", "failed", _ts(1)))
        runsdb.add_note(logs_dir, runsdb.find_run(logs_dir, "r1"), "handling", actor="jason")
        runsdb.begin_run(logs_dir, _run("r1", "failed", _ts(1)))
        assert runsdb.list_runs(logs_dir, live=False)[0]["note"]["state"] == "handling"

    def test_unwritable_index_raises(self, logs_dir, monkeypatch):
        runsdb.begin_run(logs_dir, _run("r1", "failed", _ts(1)))
        run = runsdb.find_run(logs_dir, "r1")
        monkeypatch.setattr(runsdb.os, "access", lambda p, mode: False)
        with pytest.raises(runsdb.NoteError):
            runsdb.add_note(logs_dir, run, "handling")

    def test_prune_by_age_drops_old_notes(self, logs_dir):
        runsdb.begin_run(logs_dir, _run("r1", "failed", _ts(1)))
        runsdb.add_note(logs_dir, runsdb.find_run(logs_dir, "r1"), "handling")
        con = runsdb._connect(runsdb.db_path(logs_dir))
        con.execute("UPDATE run_notes SET ts = ?", ("2000-01-01T00:00:00+00:00",))
        con.commit()
        runsdb._prune(con, 30, 0)
        con.commit()
        assert con.execute("SELECT count(*) FROM run_notes").fetchone()[0] == 0
        con.close()


# ── the CLI verbs ──────────────────────────────────────────────────────────────


class TestHistoryVerbs:
    @pytest.fixture
    def cli(self, logs_dir, monkeypatch):
        from runspec import cli
        from runspec import logs as _logs

        monkeypatch.setattr(_logs, "scope_dirs", lambda scope, destructive=False: [logs_dir])
        monkeypatch.setenv("RUNSPEC_AUDIT", "off")
        monkeypatch.delenv("SUDO_USER", raising=False)
        return cli

    def _lines(self, capsys):
        return [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]

    def test_claim_by_label_then_jobs(self, cli, logs_dir, capsys):
        runsdb.begin_run(logs_dir, _run("ok1", "completed", _ts(1)))
        runsdb.begin_run(logs_dir, _run("bad1", "failed", _ts(2)))
        runsdb.begin_run(logs_dir, _run("bad2", "failed", _ts(3)))
        cli.cmd_history(["claim", "--label", "sched:nightly", "--note", "on it", "--json"])
        out = self._lines(capsys)[-1]
        assert out["ok"] and out["notes"][0]["run_id"] == "bad2" and out["notes"][0]["state"] == "handling"
        cli.cmd_history(["jobs", "--failing", "--json"])
        job = self._lines(capsys)[-1]
        assert job["job"] == "sched:nightly" and job["state"] == "handling" and job["failures"] == 2

    def test_resolve_needs_a_note(self, cli, logs_dir, capsys):
        runsdb.begin_run(logs_dir, _run("bad1", "failed", _ts(2)))
        with pytest.raises(SystemExit):
            cli.cmd_history(["resolve", "bad1", "--json"])
        assert "note" in self._lines(capsys)[-1]["error"]

    def test_several_runs_at_once(self, cli, logs_dir, capsys):
        runsdb.begin_run(logs_dir, _run("aaa1", "failed", _ts(2), label="sched:a"))
        runsdb.begin_run(logs_dir, _run("bbb1", "failed", _ts(2), label="sched:b"))
        cli.cmd_history(["resolve", "aaa1,bbb1", "--note", "creds rotated", "--json"])
        assert [n["run_id"] for n in self._lines(capsys)[-1]["notes"]] == ["aaa1", "bbb1"]
        cli.cmd_history(["jobs", "--json"])
        assert {j["job"]: j["state"] for j in self._lines(capsys)} == {"sched:a": "done", "sched:b": "done"}
        cli.cmd_history(["notes", "--json"])
        assert len(self._lines(capsys)) == 2

    def test_claim_on_clean_job_is_refused(self, cli, logs_dir, capsys):
        runsdb.begin_run(logs_dir, _run("ok1", "completed", _ts(1)))
        with pytest.raises(SystemExit):
            cli.cmd_history(["claim", "--label", "sched:nightly", "--json"])
        assert "isn't failing" in self._lines(capsys)[-1]["error"]

    def test_unknown_run_is_refused(self, cli, logs_dir, capsys):
        with pytest.raises(SystemExit):
            cli.cmd_history(["release", "nope", "--json"])
        assert "no run 'nope'" in self._lines(capsys)[-1]["error"]

    def test_actor_is_the_sudo_user(self, cli, logs_dir, capsys, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "jason")
        runsdb.begin_run(logs_dir, _run("bad1", "failed", _ts(2)))
        cli.cmd_history(["claim", "bad1", "--json"])
        assert self._lines(capsys)[-1]["notes"][0]["actor"] == "jason"

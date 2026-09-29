"""Tests for status — the config ⋈ crontab ⋈ runs-index join behind
`runspec-scheduler status` (and runspec-console's Shared schedules view)."""

from __future__ import annotations

import json

import pytest
from runspec import runsdb

from runspec_scheduler import status as st
from runspec_scheduler.config import ScheduleEntry, write_schedules
from runspec_scheduler.timing import When


@pytest.fixture
def env(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(st.rs_logs, "log_dirs", lambda: [logs])
    cfg = tmp_path / "runspec_scheduler.toml"
    write_schedules(
        cfg,
        [
            ScheduleEntry(id="nightly", when=When(cron="0 2 * * *"), runnable="backup", args={"target": "prod"}),
            ScheduleEntry(id="often", when=When(every="15m"), runnable="report"),
        ],
    )
    return cfg, logs


def _crontab(monkeypatch, lines):
    monkeypatch.setattr(st, "_read_crontab_noninteractive", lambda user, method: lines)
    # every configured runnable counts as installed + valid unless a test says otherwise
    monkeypatch.setattr(st, "resolve_entry", lambda entry: (object(), ""))


def _run(logs, run_id, label, status="completed", exit_code=0, start="2026-09-28T02:00:00+00:00", records=()):
    log_file = f"backup.20260928T020000Z.{run_id}.log"
    runsdb.begin_run(
        logs,
        {"run_id": run_id, "runnable": "backup", "status": status, "exit_code": exit_code, "start_ts": start, "host": "h", "pid": 1, "label": label, "log_file": log_file},
    )
    with open(logs / log_file, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_parse_installed_exec_vs_legacy():
    lines = [
        "MAILTO=''",
        "# runspec:sched:new",
        "0 2 * * *  env RUNSPEC_AGENT=1 /v/bin/runspec exec --label sched:new --trigger schedule backup -- >/dev/null 2>&1",
        "# runspec:sched:old",
        "0 3 * * *  mkdir -p x && flock -n y env RUNSPEC_AGENT=1 /v/bin/backup >/dev/null 2>&1",
        "5 * * * * /usr/bin/true",
    ]
    assert st.parse_installed(lines) == {"new": "exec", "old": "legacy"}


def test_join(env, monkeypatch):
    cfg, logs = env
    _crontab(
        monkeypatch,
        [
            "# runspec:sched:nightly",
            "0 2 * * *  env RUNSPEC_AGENT=1 /v/bin/runspec exec --label sched:nightly backup -- >/dev/null 2>&1",
            "# runspec:sched:gone",
            "0 4 * * *  env RUNSPEC_AGENT=1 /v/bin/runspec exec --label sched:gone x -- >/dev/null 2>&1",
        ],
    )
    _run(logs, "aaaaaaaa-1", "sched:nightly", start="2026-09-27T02:00:00+00:00")
    _run(
        logs,
        "bbbbbbbb-2",
        "sched:nightly",
        status="failed",
        exit_code=2,
        records=[
            {"level": "ERROR", "logger": "backup", "message": "disk full", "extra": {"run_id": "bbbbbbbb-2"}},
            {"level": "INFO", "logger": "runspec.exec", "message": "captured stdout", "extra": {"run_id": "bbbbbbbb-2", "event": "exec_output", "stream": "stdout", "text": "partial\n"}},
            {"level": "ERROR", "logger": "runspec.exec", "message": "backup failed — exit 2", "extra": {"run_id": "bbbbbbbb-2", "event": "exec_result", "status": "failed", "exit_code": 2}},
            {"level": "INFO", "logger": "x", "message": "other run", "extra": {"run_id": "zzzz"}},
        ],
    )
    _run(logs, "cccccccc-3", "sched:other")  # a different schedule's run

    out = st.status(cfg)
    assert out["ok"] and out["backend"] == "cron"
    nightly, often = out["schedules"]
    assert nightly["installed"] is True and nightly["line"] == "exec"
    assert nightly["installable"] is True and nightly["next_run"]
    assert [r["run_id"] for r in nightly["runs"]] == ["bbbbbbbb-2", "aaaaaaaa-1"]  # newest first, own label only
    lo = nightly["last_output"]
    assert lo["stdout"] == "partial\n" and lo["errors"] == ["disk full"]
    assert lo["result"]["exit_code"] == 2
    # interval schedules aren't installable on the cron backend
    assert often["installable"] is False and often["installed"] is False
    assert out["stale"] == [{"id": "gone", "user": "(self)"}]


def test_unreadable_crontab_is_unknown_not_fatal(env, monkeypatch):
    cfg, _ = env
    _crontab(monkeypatch, None)
    out = st.status(cfg)
    assert out["ok"]
    assert {s["installed"] for s in out["schedules"]} == {None}


def test_missing_log_file_gives_no_output(env, monkeypatch):
    cfg, logs = env
    _crontab(monkeypatch, [])
    _run(logs, "dddddddd-4", "sched:nightly")
    (logs / "backup.20260928T020000Z.dddddddd-4.log").unlink()
    nightly = st.status(cfg)["schedules"][0]
    assert nightly["runs"][0]["run_id"] == "dddddddd-4"
    assert nightly["last_output"] is None


def test_output_is_tail_trimmed(env, monkeypatch):
    cfg, logs = env
    _crontab(monkeypatch, [])
    _run(
        logs,
        "eeeeeeee-5",
        "sched:nightly",
        records=[{"level": "INFO", "logger": "runspec.exec", "message": "m", "extra": {"run_id": "eeeeeeee-5", "event": "exec_output", "stream": "stderr", "text": "x" * 100 + "END"}}],
    )
    lo = st.status(cfg, output_bytes=10)["schedules"][0]["last_output"]
    assert lo["stderr"].endswith("END") and len(lo["stderr"]) == 10 and lo["truncated"] is True


def test_invalid_invocation_is_not_installable(env, monkeypatch):
    cfg, _ = env
    _crontab(monkeypatch, [])
    monkeypatch.setattr(st, "resolve_entry", lambda entry: (None, "unknown command 'x'"))
    nightly = st.status(cfg)["schedules"][0]
    assert nightly["installable"] is False
    assert nightly["not_installable_reason"] == "unknown command 'x'"


def test_inbox_rows_count_only_from_the_schedules_account(env, monkeypatch):
    # A crontab under another account records its runs in the venv's logs inbox
    # until the owner sweeps them; anyone could drop a file labelled sched:<id>
    # there, so only the schedule's own account's rows count.
    cfg, logs = env
    _crontab(monkeypatch, [])
    mine = {"run_id": "cccccccc-3", "status": "running", "label": "sched:nightly", "start_ts": "2026-09-28T03:00:00+00:00", "source": "inbox", "os_user": "svc", "pending": True}
    forged = {**mine, "run_id": "dddddddd-4", "os_user": "mallory", "start_ts": "2026-09-28T04:00:00+00:00"}
    monkeypatch.setattr(st, "_list_runs", lambda d, label, limit: [dict(forged), dict(mine)] if label == "sched:nightly" else [])
    write_schedules(cfg, [ScheduleEntry(id="nightly", when=When(cron="0 2 * * *"), runnable="backup", run_as="svc")])
    [rec] = st.status(cfg)["schedules"]
    assert [r["run_id"] for r in rec["runs"]] == ["cccccccc-3"]
    assert rec["runs"][0]["os_user"] == "svc" and rec["runs"][0]["pending"] is True

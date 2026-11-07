"""run-now --detach: the account a fire runs as, and starting it in the background."""

from __future__ import annotations

from pathlib import Path

from runspec_scheduler import runnow
from runspec_scheduler.config import ScheduleEntry, SchedulerSettings
from runspec_scheduler.discovery import Target
from runspec_scheduler.timing import When


def _entry(**kw):
    base = {"id": "nightly", "when": When(cron="0 2 * * *"), "runnable": "backup"}
    base.update(kw)
    return ScheduleEntry(**base)


def _target(run_as=""):
    return Target(runnable="backup", source="/v/x/runspec.toml", binary=Path("/v/bin/backup"), run_as=run_as, become_method="sudo", become_flags=None, spec={"args": {}})


def _resolved(monkeypatch, target):
    monkeypatch.setattr(runnow, "resolve_entry", lambda e: (target, "") if target else (None, "runnable 'backup' not installed in this venv"))
    monkeypatch.setattr(runnow, "_me", lambda: "owner")


def test_cron_fire_runs_as_the_schedules_account(monkeypatch):
    _resolved(monkeypatch, _target(run_as="svc"))
    p = runnow.plan(_entry(), SchedulerSettings(backend="cron"))
    assert p["user"] == "svc"
    assert p["argv"][:5] == ["/v/bin/runspec", "exec", "--label", "sched:nightly", "--trigger"]
    assert "--lock" in p["argv"]
    assert p["argv"][p["argv"].index("--trigger") + 1] == "schedule:run-now"
    # the schedule's own run_as wins over the runnable's
    assert runnow.plan(_entry(run_as="ops"), SchedulerSettings(backend="cron"))["user"] == "ops"


def test_daemon_fire_runs_as_the_owner_with_its_timeout(monkeypatch):
    _resolved(monkeypatch, _target(run_as="svc"))
    p = runnow.plan(_entry(), SchedulerSettings(backend="daemon", timeout=600))
    assert p["user"] == ""
    assert p["argv"][p["argv"].index("--timeout") + 1] == "600"


def test_running_as_yourself_needs_no_escalation(monkeypatch):
    _resolved(monkeypatch, _target(run_as="owner"))
    assert runnow.plan(_entry(), SchedulerSettings(backend="cron"))["user"] == ""


def test_not_installed_is_an_error(monkeypatch):
    _resolved(monkeypatch, None)
    out = runnow.start(_entry(), SchedulerSettings())
    assert out == {"ok": False, "id": "nightly", "error": "schedule 'nightly' can't run: runnable 'backup' not installed in this venv"}


def test_start_reports_a_fire_that_ends_at_once(monkeypatch, tmp_path):
    seen = tmp_path / "env"
    monkeypatch.setattr(
        runnow,
        "plan",
        lambda e, s: {"argv": ["sh", "-c", f'echo "$RUNSPEC_AGENT" > {seen}; exit 3'], "env": {"RUNSPEC_AGENT": "1"}, "user": "", "become_method": "sudo", "become_flags": "", "home": str(tmp_path)},
    )
    monkeypatch.setenv("RUNSPEC_RUN_ID", "parent-run")
    out = runnow.start(_entry(), SchedulerSettings(), settle=3)
    assert out["ok"] and out["finished"] and out["exit_code"] == 3 and out["pid"] > 0
    assert seen.read_text().strip() == "1"


def test_start_returns_while_a_long_fire_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runnow,
        "plan",
        lambda e, s: {"argv": ["sleep", "5"], "env": {}, "user": "", "become_method": "sudo", "become_flags": "", "home": str(tmp_path)},
    )
    out = runnow.start(_entry(), SchedulerSettings(), settle=0.2)
    assert out["ok"] and "finished" not in out
    import os
    import signal

    os.killpg(out["pid"], signal.SIGTERM)


def test_refused_escalation_starts_nothing(monkeypatch):
    monkeypatch.setattr(
        runnow,
        "plan",
        lambda e, s: {"argv": ["true"], "env": {"RUNSPEC_AGENT": "1"}, "user": "svc", "become_method": "sudo", "become_flags": "", "home": "/home/svc"},
    )

    class Done:
        returncode = 1
        stdout = ""
        stderr = "sudo: a password is required\n"

    calls = []
    monkeypatch.setattr(runnow.subprocess, "run", lambda argv, **kw: calls.append(argv) or Done())
    monkeypatch.setattr(runnow.subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("started")))
    out = runnow.start(_entry(), SchedulerSettings())
    assert out == {"ok": False, "id": "nightly", "error": "can't run as svc: sudo: a password is required"}
    assert calls[0][:4] == ["sudo", "-n", "-u", "svc"]

"""Readable terminal output: render.py's text, and when the cli picks it."""

from __future__ import annotations

import datetime as dt
import io
import json
import sys

import pytest

from runspec_scheduler import cli, render

NOW = dt.datetime(2026, 11, 9, 12, 0, tzinfo=dt.timezone.utc)


def _iso(**delta: float) -> str:
    return (NOW + dt.timedelta(**delta)).isoformat()


# ── helpers ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (45, "45s"), (60, "1m"), (200, "3m 20s"), (3600, "1h"), (21600, "6h"), (3 * 3600 + 120, "3h 2m"), (86400, "1d"), (2 * 86400 + 4 * 3600 + 60, "2d 4h")],
)
def test_span(seconds, text):
    assert render.span(seconds) == text


def test_duration_zero_means_no_limit():
    assert render.duration(0) == "no limit"
    assert render.duration(300) == "5m"
    assert render.duration(None) == "-"


def test_run_time():
    assert render.run_time(1234) == "1.2s"
    assert render.run_time(125_000) == "2m 5s"
    assert render.run_time(None) == ""


def test_ago_and_next():
    assert render.ago(_iso(seconds=-2), NOW) == "just now"
    assert render.ago(_iso(minutes=-3), NOW) == "3m ago"
    assert render.when_next(_iso(minutes=25), NOW) == "12:25 (in 25m)"
    assert render.when_next(_iso(hours=14), NOW) == "tomorrow 02:00 (in 14h)"
    assert render.when_next(_iso(days=3), NOW) == "Thu 12 Nov 12:00 (in 3d)"
    assert render.when_next(None, NOW) == "-"


def test_schedule_when():
    assert render.schedule_when({"cron": "0 2 * * *"}) == "0 2 * * *"
    assert render.schedule_when({"every": "30s"}) == "every 30s"
    assert render.schedule_when({"cron": "@daily", "timezone": "Europe/London"}) == "@daily (Europe/London)"


def test_table_cuts_all_but_the_last_column():
    long = "x" * 80
    lines = render.table(["a", "b"], [[long, long]])
    assert lines[0].startswith("A")
    assert "…" in lines[1]
    assert lines[1].endswith(long)


# ── status ────────────────────────────────────────────────────────────────────


def _status(**over):
    payload = {
        "ok": True,
        "venv": "ops-tools",
        "config": "/srv/ops-tools/runspec_scheduler.toml",
        "backend": "daemon",
        "settings": {"backend": "daemon", "timeout": 21600, "misfire_grace": 300, "catch_up_window": 86400},
        "daemon": {"state": "running", "pid": 4242, "uptime_s": 3 * 3600 + 120, "version": "0.8.0", "restart_needed": False, "running": [], "config_error": None},
        "keeper": [{"manager": "systemd-user", "where": "/home/svc/.config/systemd/user/x.service"}],
        "schedules": [
            {
                "id": "nightly-backup",
                "runnable": "backup",
                "enabled": True,
                "when": {"cron": "0 2 * * *"},
                "installable": True,
                "installed": True,
                "next_run": _iso(hours=14),
                "runs": [{"status": "completed", "exit_code": 0, "start_ts": _iso(hours=-10), "duration_ms": 12_000}],
                "attention": {"state": "clean", "failures": 0},
            },
            {
                "id": "tmp-usage",
                "runnable": "filesystem",
                "command": ["directory-size"],
                "enabled": True,
                "when": {"every": "30m"},
                "installable": True,
                "installed": True,
                "next_run": _iso(minutes=25),
                "runs": [{"status": "failed", "exit_code": 1, "start_ts": _iso(minutes=-5), "duration_ms": 800}],
                "attention": {"state": "handling", "failures": 3, "since": _iso(hours=-1), "note": {"actor": "jason", "note": "disk filling, cleaning up"}},
                "last_output": {"stdout": "", "stderr": "Traceback\nOSError: No space left on device", "errors": []},
            },
            {
                "id": "london-report",
                "runnable": "report",
                "enabled": False,
                "when": {"cron": "@daily", "timezone": "Europe/London"},
                "installable": True,
                "installed": True,
                "next_run": None,
                "runs": [],
                "attention": {"state": "clean"},
            },
        ],
        "stale": [],
        "errors": [],
    }
    payload.update(over)
    return payload


def test_status_text():
    text = render.status(_status(), NOW)
    assert "ops-tools" in text
    assert "running (pid 4242, up 3h 2m), version 0.8.0" in text
    assert "systemd-user" in text
    assert "timeout 6h, misfire grace 5m, catch-up window 1d" in text
    rows: dict[str, str] = {}
    for line in text.splitlines():  # the table row, not the "Needs attention" heading
        if line.split() and line.split()[0] in ("nightly-backup", "tmp-usage", "london-report"):
            rows.setdefault(line.split()[0], line)
    assert "ok, 10h ago, 12s" in rows["nightly-backup"]
    assert rows["nightly-backup"].rstrip().endswith("ok")
    assert "filesystem directory-size" in rows["tmp-usage"]
    assert "failed (exit 1), 5m ago, 0.8s" in rows["tmp-usage"]
    assert "being handled by jason" in rows["tmp-usage"]
    assert rows["london-report"].rstrip().endswith("paused")
    # A failing schedule gets its last output and who is on it.
    assert "Needs attention:" in text
    assert "jason is on it: disk filling, cleaning up" in text
    assert "| OSError: No space left on device" in text
    assert "{" not in text  # no JSON


def test_status_daemon_states():
    stopped = render.status(_status(daemon={"state": "stopped"}), NOW)
    assert "not running" in stopped
    stale = render.status(_status(daemon={"state": "stale", "pid": 1, "heartbeat_age_s": 40}), NOW)
    assert "not responding (no heartbeat for 40s)" in stale
    restart = render.status(_status(daemon={**_status()["daemon"], "restart_needed": True}), NOW)
    assert "runspec-scheduler restart" in restart


def test_status_on_cron():
    payload = _status(backend="cron", daemon={"state": "stopped"}, keeper=[])
    payload["schedules"][0].update(installed=False, line=None)
    text = render.status(payload, NOW)
    assert "Daemon" not in text  # nothing to say about a daemon cron doesn't use
    assert "Settings" not in text
    assert "not in crontab (run sync)" in text


def test_status_stale_lines_and_errors():
    text = render.status(_status(stale=[{"id": "old", "user": "svc", "reason": "not in the config"}], errors=["could not read x"]), NOW)
    assert "run sync to remove them" in text
    assert "old (svc): not in the config" in text
    assert "could not read x" in text


def test_status_no_schedules():
    assert "No schedules yet" in render.status(_status(schedules=[]), NOW)


# ── the other verbs ───────────────────────────────────────────────────────────


def test_list():
    payload = {
        "venv": "v",
        "config": "/c",
        "backend": "cron",
        "schedules": [{"id": "a", "runnable": "r", "enabled": True, "when": {"cron": "0 2 * * *"}, "next_run": _iso(hours=14), "run_as": "svc", "args": {"path": "/tmp"}}],
    }
    text = render.schedules(payload, NOW)
    assert "tomorrow 02:00" in text
    assert "path=/tmp" in text
    assert "svc" in text


def test_added_hints_at_sync_on_cron():
    assert "runspec-scheduler sync" in render.added({"id": "a", "runnable": "r", "command": [], "backend": "cron"})
    assert "picks it up" in render.added({"id": "a", "runnable": "r", "command": ["x"], "backend": "daemon"})


def test_removed():
    assert render.removed({"ok": False, "id": "a"}) == "There's no schedule a in this venv."
    assert "sync" in render.removed({"ok": True, "id": "a", "backend": "cron"})


def test_ran_and_started():
    text = render.ran({"id": "a", "runnable": "r", "exit_code": 2, "stdout": "hi", "stderr": "bad"})
    assert "failed with exit code 2" in text
    assert "  hi" in text and "  bad" in text
    assert "pid 7" in render.started({"id": "a", "user": "svc", "pid": 7})
    assert "finished OK" in render.started({"id": "a", "finished": True, "exit_code": 0})


def test_synced():
    text = render.synced({"ok": True, "dry_run": False, "backend": "cron", "installed": [{"id": "a", "runnable": "r", "user": "svc"}], "skipped": [{"id": "b", "reason": "disabled"}], "errors": []})
    assert "Wrote 1 crontab line:" in text
    assert "b: disabled" in text
    assert "Would write" in render.synced({"dry_run": True, "backend": "cron", "installed": [{"id": "a"}]})


def test_used():
    assert "now uses daemon (was cron)" in render.used({"ok": True, "backend": "daemon", "previous": "cron", "daemon": {"state": "running"}})
    failed = render.used({"ok": False, "backend": "cron", "previous": "cron", "error": "could not remove", "crontab": {"errors": [{"user": "svc", "error": "denied"}]}})
    assert "could not remove" in failed
    assert "svc's crontab: denied" in failed


def test_settings():
    text = render.settings({"config": "/c", "changed": ["misfire_grace"], "settings": {"backend": "daemon", "timeout": 0, "misfire_grace": 600, "catch_up_window": 86400}})
    assert "no limit" in text
    assert "10m" in text
    assert "Changed misfire grace" in text


def test_plan():
    text = render.plan(
        {
            "ok": True,
            "manager": "systemd",
            "reason": "root installs it",
            "written": False,
            "steps": ["write the unit"],
            "files": [{"path": "/etc/systemd/system/x.service", "content": "[Unit]\nDescription=x", "by": "root"}],
            "root_steps": ["systemctl enable --now x"],
            "warnings": [],
            "hint": "nothing was changed",
        }
    )
    assert "Would do:" in text
    assert "    [Unit]" in text  # root has to write it, so the content is shown
    assert "For root to do:" in text
    done = render.plan({"ok": True, "manager": "cron", "written": True, "steps": ["a", "b"], "done": ["a", "b"], "daemon": {"state": "running", "pid": 9}})
    assert "✓ a" in done and "pid 9" in done


# ── when the cli prints text ──────────────────────────────────────────────────


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def terminal(monkeypatch):
    out = _Tty()
    monkeypatch.setattr(sys, "__stdout__", out)
    monkeypatch.setattr(cli, "_FORCE_JSON", False)
    return out


def test_json_when_not_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(sys, "__stdout__", io.StringIO())
    monkeypatch.setattr(cli, "_FORCE_JSON", False)
    cli._emit({"ok": True, "id": "a"}, render.removed)
    assert json.loads(capsys.readouterr().out) == {"ok": True, "id": "a"}


def test_text_at_a_terminal(terminal, capsys):
    cli._emit({"ok": True, "id": "a", "backend": "daemon"}, render.removed)
    assert capsys.readouterr().out.strip() == "Removed schedule a."


def test_json_flag_wins_at_a_terminal(terminal, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_FORCE_JSON", True)
    cli._emit({"ok": True, "id": "a"}, render.removed)
    assert json.loads(capsys.readouterr().out)["id"] == "a"


def test_errors_go_to_stderr_at_a_terminal(terminal, capsys):
    cli._emit({"error": "no schedule with id 'x'"})
    got = capsys.readouterr()
    assert got.out == ""
    assert got.err.strip() == "runspec-scheduler: no schedule with id 'x'"

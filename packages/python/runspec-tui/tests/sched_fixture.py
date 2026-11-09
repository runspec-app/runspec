"""A ``runspec-scheduler status`` payload and a fake scheduler for the
Schedules screen tests (no real scheduler, sudo or crontab)."""

from __future__ import annotations

import copy
import datetime as _dt
from pathlib import Path
from typing import Any

from runspec_tui import schedules as sch

NOW = _dt.datetime(2026, 11, 9, 9, 0, tzinfo=_dt.timezone.utc)


def _iso(delta_h: float) -> str:
    return (NOW + _dt.timedelta(hours=delta_h)).isoformat()


STATUS: dict[str, Any] = {
    "ok": True,
    "venv": "ops",
    "config": "/opt/venvs/ops/runspec_scheduler.toml",
    "backend": "cron",
    "settings": {"backend": "cron", "timeout": None, "misfire_grace": 300, "catch_up_window": 86400, "timezone": "", "package_schedules": "offer"},
    "host_zone": "Europe/London",
    "packages": {"policy": "offer", "offered": 1, "errors": [], "orphans": []},
    "daemon": {"state": "stopped"},
    "keeper": [],
    "stale": [],
    "errors": [],
    "schedules": [
        {
            "id": "nightly-backup",
            "runnable": "db",
            "command": ["backup"],
            "args": {"target": "/backups"},
            "enabled": True,
            "when": {"cron": "30 2 * * *"},
            "installed": True,
            "installable": True,
            "next_run": _iso(17.5),
            "runs": [
                {"run_id": "r3", "status": "failed", "exit_code": 3, "start_ts": _iso(-6.5), "duration_ms": 1200},
                {"run_id": "r2", "status": "completed", "exit_code": 0, "start_ts": _iso(-30.5), "duration_ms": 900},
            ],
            "attention": {"state": "failed", "failures": 1},
            "last_output": {"stdout": "", "stderr": "disk full\n"},
        },
        {
            "id": "deploy-weekly",
            "runnable": "deploy",
            "enabled": False,
            "when": {"cron": "0 6 * * mon"},
            "installed": False,
            "installable": True,
            "next_run": None,
            "runs": [],
            "attention": {"state": "clean"},
        },
        {
            "id": "cleanup-tmp.nightly",
            "runnable": "cleanup-tmp",
            "enabled": False,
            "offered": True,
            "origin": "package",
            "description": "Old files out of /tmp",
            "when": {"cron": "51 2 * * *", "timezone": "local"},
            "package": {"words": "02:30 daily, spread 30m (this host 02:51)", "overridden": []},
            "installed": False,
            "installable": True,
            "next_run": None,
            "runs": [],
            "attention": {"state": "clean"},
        },
    ],
}


class FakeScheduler:
    """Answers ``status`` from :data:`STATUS` and records every other call."""

    def __init__(self, status: dict[str, Any] | None = None, fail: dict[str, str] | None = None) -> None:
        self.status = copy.deepcopy(status or STATUS)
        self.fail = fail or {}
        self.calls: list[sch.Command] = []

    def verb(self, cmd: sch.Command) -> str:
        argv = cmd.argv
        if "history" in argv:
            return "history " + argv[argv.index("history") + 1]
        i = next(i for i, a in enumerate(argv) if a.endswith("runspec-scheduler"))
        return argv[i + 1]

    def __call__(self, cmd: sch.Command) -> sch.Result:
        verb = self.verb(cmd)
        if verb == "status":
            return sch.Result(0, copy.deepcopy(self.status))
        self.calls.append(cmd)
        if verb in self.fail:
            return sch.Result(2, {"ok": False, "error": self.fail[verb]})
        return sch.Result(0, {"ok": True, "backend": self.status["backend"]})

    def made(self) -> list[str]:
        return [self.verb(c) for c in self.calls]


CALLER = sch.Caller(Path("/opt/venvs/ops/bin/runspec-scheduler"), owner="ops", user="ops")

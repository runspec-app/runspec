"""Scheduling a runnable's subcommand (verb): the `command` field round-trips
through the config, is validated against the runnable's spec, and lands on the
crontab line / run argv in the documented order (globals, verb path, verb args)."""

from __future__ import annotations

import pytest

from runspec_scheduler import cronsync
from runspec_scheduler.config import ScheduleEntry, load_schedules, parse_command, write_schedules
from runspec_scheduler.discovery import Target, check_invocation
from runspec_scheduler.invocation import plan_invocation
from runspec_scheduler.timing import When

# A normalised spec shaped like runspec.loader output: globals + nested verbs.
SPEC = {
    "args": {"run-as": {}, "become-method": {}},
    "require_command": True,
    "commands": {
        "list-directory": {"args": {"path": {}}, "commands": {}, "require_command": False},
        "admin": {
            "args": {"verbose": {}},
            "require_command": True,
            "commands": {"users": {"args": {"limit": {}}, "commands": {}, "require_command": False}},
        },
    },
}
FLAT = {"args": {"target": {}}, "commands": {}, "require_command": False}


class TestParseCommand:
    def test_forms(self):
        assert parse_command(None) == ()
        assert parse_command("") == ()
        assert parse_command("admin users") == ("admin", "users")
        assert parse_command(["admin", " users "]) == ("admin", "users")

    def test_rejects_other_shapes(self):
        with pytest.raises(ValueError):
            parse_command(3)


class TestCheckInvocation:
    def test_valid_leaf_and_nested(self):
        assert check_invocation(SPEC, ("list-directory",), {"path": "/tmp", "run_as": "svc"}) is None
        assert check_invocation(SPEC, ("admin", "users"), {"verbose": True, "limit": 5}) is None
        assert check_invocation(FLAT, (), {"target": "prod"}) is None

    def test_missing_required_command_names_choices(self):
        err = check_invocation(SPEC, (), {})
        assert err and "needs a command" in err and "admin, list-directory" in err
        err = check_invocation(SPEC, ("admin",), {})
        assert err and "'admin' needs a command" in err and "users" in err

    def test_unknown_command(self):
        err = check_invocation(SPEC, ("bogus",), {})
        assert err and "unknown command 'bogus'" in err and "list-directory" in err

    def test_command_on_flat_runnable(self):
        err = check_invocation(FLAT, ("list",), {})
        assert err and "has no subcommands" in err

    def test_unknown_arg_for_that_verb(self):
        err = check_invocation(SPEC, ("list-directory",), {"limit": 5})
        assert err and "unknown argument(s) limit" in err and "path" in err


class TestArgv:
    def test_globals_then_verb_then_verb_args(self):
        plan = plan_invocation("filesystem", SPEC, ("list-directory",), {"path": "/tmp", "run_as": "svc"})
        assert plan.argv == ["--run-as", "svc", "list-directory", "--path", "/tmp"]

    def test_no_command_is_just_args(self):
        assert plan_invocation("backup", FLAT, (), {"target": "prod"}).argv == ["--target", "prod"]

    def test_crontab_line_carries_the_verb(self):
        cmd = cronsync.render_command(
            "/v/bin/runspec",
            "filesystem",
            {"path": "/tmp"},
            "",
            home="/home/svc",
            sched_id="t",
            command=("list-directory",),
            spec=SPEC,
        )
        assert "filesystem -- list-directory --path /tmp >/dev/null" in cmd


class TestConfig:
    def test_command_round_trips(self, tmp_path):
        path = tmp_path / "s.toml"
        write_schedules(path, [ScheduleEntry(id="a", when=When(cron="0 * * * *"), runnable="filesystem", command=("admin", "users"))])
        assert 'command = ["admin", "users"]' in path.read_text()
        [e] = load_schedules(path)
        assert e.command == ("admin", "users")
        assert e.to_dict()["command"] == ["admin", "users"]

    def test_no_command_writes_no_key(self, tmp_path):
        path = tmp_path / "s.toml"
        write_schedules(path, [ScheduleEntry(id="a", when=When(cron="0 * * * *"), runnable="backup")])
        assert "command" not in path.read_text()
        assert "command" not in load_schedules(path)[0].to_dict()


def _target(spec):
    from pathlib import Path

    return Target(runnable="filesystem", source="", binary=Path("/v/bin/filesystem"), run_as="", become_method="sudo", become_flags=None, spec=spec)


class TestSync:
    def test_invalid_invocation_is_skipped_not_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cronsync, "resolve_target", lambda name: _target(SPEC))
        monkeypatch.setattr(cronsync, "_read_crontab", lambda *a: [])
        written = {}
        monkeypatch.setattr(cronsync, "_write_crontab", lambda lines, *a: written.setdefault("lines", lines))
        write_schedules(
            tmp_path / "c.toml",
            [
                ScheduleEntry(id="ok", when=When(cron="0 * * * *"), runnable="filesystem", command=("list-directory",), args={"path": "/tmp"}),
                ScheduleEntry(id="bad", when=When(cron="0 * * * *"), runnable="filesystem"),  # needs a verb
            ],
        )
        res = cronsync.sync(tmp_path / "c.toml")
        assert [r["id"] for r in res.installed] == ["ok"]
        assert res.skipped == [{"id": "bad", "reason": "this runnable needs a command (--command); choose one of: admin, list-directory"}]
        job = next(line for line in written["lines"] if "exec" in line)
        assert "filesystem -- list-directory --path /tmp" in job

from __future__ import annotations

import datetime as _dt
import json
import subprocess
from pathlib import Path

import pytest
from conftest import fixture_runnables
from sched_fixture import CALLER, NOW, STATUS, FakeScheduler
from textual.widgets import DataTable, Input, Select, Static

from runspec_tui import schedules as sch
from runspec_tui.app import RunspecTUI
from runspec_tui.schedules_screen import ConfirmScreen, NoteScreen, SchedulesScreen, SettingsScreen, TimingScreen

SIZE = (140, 48)
BIN = Path("/opt/venvs/ops/bin/runspec-scheduler")


# ── commands ──────────────────────────────────────────────────────────────────


def test_reads_run_as_you_and_writes_as_the_owner() -> None:
    caller = sch.Caller(BIN, owner="svc", user="alice")
    assert caller.command("status").argv == [str(BIN), "status", "--json"]
    write = caller.command("enable", "--id", "x")
    assert write.argv[:4] == ["sudo", "-n", "-u", "svc"] and write.argv[-4:] == [str(BIN), "enable", "--id", "x", "--json"][-4:]
    assert write.stdin is None


def test_the_owner_runs_writes_directly() -> None:
    cmd = CALLER.command("add", "--id", "x", env={sch.ENABLED_ENV: "0"})
    assert cmd.argv[0] == str(BIN) and cmd.env == {sch.ENABLED_ENV: "0"} and cmd.stdin is None


def test_variables_through_sudo_go_on_stdin_not_argv() -> None:
    caller = sch.Caller(BIN, owner="svc", user="alice")
    cmd = caller.command("add", "--id", "x", env={sch.ENABLED_ENV: "0"})
    assert cmd.argv[:4] == ["sudo", "-n", "-u", "svc"]
    assert not any(sch.ENABLED_ENV in a for a in cmd.argv)
    assert cmd.stdin and sch.ENABLED_ENV.encode() in cmd.stdin


def test_notes_go_through_runspec_history_as_the_owner() -> None:
    caller = sch.Caller(BIN, owner="svc", user="alice")
    cmd = caller.history("resolve", "--label", "sched:x", "--note", "fixed")
    assert cmd.argv[:4] == ["sudo", "-n", "-u", "svc"]
    assert cmd.argv[4:] == [str(BIN.with_name("runspec")), "history", "resolve", "--label", "sched:x", "--note", "fixed", "--scope", "venv", "--json"]


def test_call_reads_the_last_json_line() -> None:
    def run(argv, **kw):
        assert kw["input"] == b""
        return subprocess.CompletedProcess(argv, 0, b'starting\n{"ok": true, "n": 2}\n', b"")

    result = sch.call(sch.Command(["x"]), run=run)
    assert result.ok and result.payload == {"ok": True, "n": 2}


def test_call_explains_a_sudo_password_prompt() -> None:
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, b"", b"sudo: a password is required\n")

    result = sch.call(sch.Command(["sudo", "x"]), run=run)
    assert not result.ok and "passwordless sudo rule" in result.message


def test_call_reports_errors_from_json_and_a_missing_binary() -> None:
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 2, json.dumps({"error": "no such schedule"}).encode(), b"")

    assert sch.call(sch.Command(["x"]), run=run).message == "no such schedule"

    def missing(argv, **kw):
        raise FileNotFoundError("runspec-scheduler")

    assert sch.call(sch.Command(["x"]), run=missing).code == 127


# ── what rows show ────────────────────────────────────────────────────────────


def rec(sid: str) -> dict:
    return next(r for r in STATUS["schedules"] if r["id"] == sid)


def test_when_uses_the_package_words_until_the_venv_changes_the_time() -> None:
    assert sch.when_text(rec("cleanup-tmp.nightly")) == "02:30 daily, spread 30m (this host 02:51)"
    changed = {**rec("cleanup-tmp.nightly"), "when": {"cron": "0 4 * * *"}, "package": {"words": "02:30 daily", "overridden": ["when"]}}
    assert sch.when_text(changed) == "0 4 * * *"
    assert sch.when_text({"when": {"every": "15m", "timezone": "UTC"}}) == "every 15m (UTC)"


def test_next_and_last() -> None:
    assert sch.next_text(rec("nightly-backup"), NOW) == "tomorrow 02:30 (in 17h 30m)"
    assert sch.next_text(rec("deploy-weekly"), NOW) == "-"
    assert sch.last_text(rec("nightly-backup"), NOW) == "failed (exit 3), 6h 30m ago"
    assert sch.last_text(rec("deploy-weekly"), NOW) == "never"


@pytest.mark.parametrize(
    ("sid", "changes", "state"),
    [
        ("nightly-backup", {}, "failing"),
        ("nightly-backup", {"attention": {"state": "handling", "note": {"actor": "ana"}}}, "being handled by ana"),
        ("nightly-backup", {"attention": {"state": "done"}}, "dealt with"),
        ("nightly-backup", {"attention": {"state": "clean"}, "installed": False}, "not in crontab"),
        ("nightly-backup", {"attention": {"state": "clean"}, "installable": False}, "won't run"),
        ("deploy-weekly", {}, "paused"),
        ("cleanup-tmp.nightly", {}, "offered"),
    ],
)
def test_state(sid, changes, state) -> None:
    assert sch.state_text({**rec(sid), **changes}, "cron")[0] == state


def test_daemon_and_zone_words() -> None:
    assert sch.daemon_text(STATUS) == ("not used (cron)", "dim")
    running = {"backend": "daemon", "daemon": {"state": "running", "pid": 42, "version": "0.10.0", "restart_needed": True}}
    assert sch.daemon_text(running)[0] == "running, pid 42, 0.10.0 — restart needed"
    assert sch.zone_text(STATUS) == "Europe/London (host)"
    assert sch.zone_text({"settings": {"timezone": "Australia/Sydney"}}) == "Australia/Sydney (venv)"


def test_detail_lines_show_runs_output_and_failure() -> None:
    lines = [t for t, _ in sch.detail_lines(rec("nightly-backup"), NOW)]
    assert lines[0] == "nightly-backup  ·  db backup"
    assert "Failing — 1 failed run" in lines
    assert any("failed (exit 3)" in t for t in lines)
    assert "  | disk full" in lines


# ── adding and editing ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "value", "zone", "expected"),
    [
        ("daily", "2:30", "", ["--cron", "30 2 * * *"]),
        ("weekdays", "07:05", "UTC", ["--cron", "5 7 * * mon-fri", "--timezone", "UTC"]),
        ("cron", "@daily", "", ["--cron", "@daily"]),
        ("every", "15M", "", ["--every", "15m"]),
    ],
)
def test_timing_args(kind, value, zone, expected) -> None:
    assert sch.timing_args(kind, value, zone) == (expected, "")


@pytest.mark.parametrize(("kind", "value"), [("daily", "25:00"), ("daily", "noon"), ("cron", "0 2 *"), ("every", "soon")])
def test_timing_args_refuses(kind, value) -> None:
    assert sch.timing_args(kind, value)[1]


@pytest.mark.parametrize(
    ("when", "prefill"),
    [
        ({"cron": "30 2 * * *"}, ("daily", "02:30", "")),
        ({"cron": "5 7 * * mon-fri", "timezone": "UTC"}, ("weekdays", "07:05", "UTC")),
        ({"cron": "0 6 * * mon", "timezone": "local"}, ("cron", "0 6 * * mon", "")),
        ({"every": "15m"}, ("every", "15m", "")),
    ],
)
def test_timing_prefill_round_trips(when, prefill) -> None:
    assert sch.timing_prefill({"when": when}) == prefill


def test_add_argv_keeps_the_rest_and_off_stays_off() -> None:
    argv, env = sch.add_argv(rec("nightly-backup"), ["--cron", "0 3 * * *"], backend="cron")
    assert argv == ["--id", "nightly-backup", "--runnable", "db", "--command", "backup", "--args", '{"target":"/backups"}', "--cron", "0 3 * * *"]
    assert env == {}
    _, env = sch.add_argv(rec("cleanup-tmp.nightly"), [], backend="cron")
    assert env == {sch.ENABLED_ENV: "0"}


def test_add_argv_leaves_the_daemon_default_timeout_alone() -> None:
    row = {**rec("deploy-weekly"), "timeout": 21600, "catch_up": True}
    argv, _ = sch.add_argv(row, [], backend="daemon", default_timeout=21600)
    assert "--timeout" not in argv and "--catch-up" in argv
    argv, _ = sch.add_argv({**row, "timeout": 600}, [], backend="daemon", default_timeout=21600)
    assert argv[argv.index("--timeout") + 1] == "600"


def test_editing_a_schedule_with_credentials_is_refused() -> None:
    assert sch.edit_problem({"env_file": "/home/svc/.x"})
    assert sch.edit_problem(rec("nightly-backup")) == ""


def test_schedule_args_skips_defaults_and_refuses_secrets() -> None:
    args = [
        {"name": "target", "type": "str", "default": "/backups"},
        {"name": "keep", "type": "int", "default": 7},
        {"name": "token", "type": "password", "required": False},
        {"name": "verbose", "type": "flag", "default": False},
    ]
    out, problems = sch.schedule_args(args, {"target": "/backups", "keep": "14", "token": "", "verbose": True})
    assert out == {"keep": "14", "verbose": True} and problems == []
    _, problems = sch.schedule_args(args, {"token": "s3cret"})
    assert problems and "runspec_schedules.env" in problems[0]


def test_ids() -> None:
    assert sch.suggest_id("db", ("backup",), {"db-backup"}) == "db-backup-2"
    assert sch.id_problem("db backup") and not sch.id_problem("db.backup_1")


def test_settings_argv_sends_only_changes() -> None:
    before = STATUS["settings"]
    assert sch.settings_argv(before, {"timezone": "", "package_schedules": "offer"}) == []
    after = {"timezone": "Australia/Sydney", "package_schedules": "auto", "timeout": "6h"}
    assert sch.settings_argv(before, after) == ["--timeout", "6h", "--timezone", "Australia/Sydney", "--package-schedules", "auto"]
    assert sch.settings_argv({"timezone": "UTC"}, {"timezone": ""}) == ["--timezone", "host"]


# ── the screen ────────────────────────────────────────────────────────────────


def make_app(fake: FakeScheduler, **kw) -> RunspecTUI:
    return RunspecTUI(fixture_runnables(), venv="/venv", user="ops", scheduler=CALLER, scheduler_call=fake, **kw)


async def open_screen(pilot) -> SchedulesScreen:
    await pilot.press("f3")
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    screen = pilot.app.screen
    assert isinstance(screen, SchedulesScreen)
    return screen


async def settle(pilot) -> None:
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def test_no_scheduler_no_f3() -> None:
    app = RunspecTUI(fixture_runnables(), venv="/venv", user="ops")
    assert app.check_action("schedules", ()) is False


async def test_lists_schedules_with_offered_last() -> None:
    fake = FakeScheduler()
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        screen = await open_screen(pilot)
        table = screen.query_one("#sched-table", DataTable)
        assert [str(k.value) for k in table.rows] == ["deploy-weekly", "nightly-backup", "cleanup-tmp.nightly"]
        strip = str(screen.query_one("#sched-strip", Static).render())
        assert "backend cron" in strip and "1 waiting to be turned on" in strip
        detail = str(screen.query_one("#sched-detail", Static).render())
        assert "deploy-weekly" in detail
        await pilot.press("escape")
        assert not isinstance(app.screen, SchedulesScreen)


async def test_turning_one_on_syncs_the_crontab() -> None:
    fake = FakeScheduler()
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await open_screen(pilot)
        await pilot.press("down", "down", "o")
        await settle(pilot)
        assert fake.made() == ["enable", "sync"]
        assert fake.calls[0].argv[-3:] == ["--id", "cleanup-tmp.nightly", "--json"]


async def test_pausing_on_the_daemon_needs_no_sync() -> None:
    status = {**STATUS, "backend": "daemon"}
    fake = FakeScheduler(status)
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await open_screen(pilot)
        await pilot.press("down", "o")
        await settle(pilot)
        assert fake.made() == ["disable"]


async def test_a_failure_shows_its_reason() -> None:
    fake = FakeScheduler(fail={"enable": "nope, not installed"})
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await open_screen(pilot)
        await pilot.press("o")
        await settle(pilot)
        assert fake.made() == ["enable"]
        assert any("nope, not installed" in str(n.message) for n in app._notifications)


async def test_run_now_asks_first() -> None:
    fake = FakeScheduler()
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await open_screen(pilot)
        await pilot.press("down", "n")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await settle(pilot)
        assert fake.made() == ["run-now"]
        assert fake.calls[0].argv[-4:] == ["--id", "nightly-backup", "--detach", "--json"]


async def test_edit_time_saves_with_add() -> None:
    fake = FakeScheduler()
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await open_screen(pilot)
        await pilot.press("down", "e")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, TimingScreen)
        assert dialog.query_one("#value", Input).value == "02:30"
        dialog.query_one("#value", Input).value = "03:15"
        await pilot.press("enter")
        await settle(pilot)
        assert fake.made() == ["add", "sync"]
        argv = fake.calls[0].argv
        assert argv[argv.index("--cron") + 1] == "15 3 * * *" and "--command" in argv


async def test_dealt_with_needs_a_note() -> None:
    fake = FakeScheduler()
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await open_screen(pilot)
        await pilot.press("down", "x")
        await pilot.pause()
        assert isinstance(app.screen, NoteScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, NoteScreen)  # still open: a note is required
        app.screen.query_one("#note", Input).value = "freed disk"
        await pilot.press("enter")
        await settle(pilot)
        assert fake.made() == ["history resolve"]
        assert fake.calls[0].argv[-7:] == ["--label", "sched:nightly-backup", "--note", "freed disk", "--scope", "venv", "--json"]


async def test_keys_follow_the_row() -> None:
    fake = FakeScheduler()
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        screen = await open_screen(pilot)
        assert screen.check_action("claim", ()) is False  # deploy-weekly: nothing failing
        assert screen.check_action("sync", ()) is True and screen.check_action("restart", ()) is False
        await pilot.press("down")
        assert screen.check_action("claim", ()) is True


async def test_settings_dialog_changes_the_zone_and_policy() -> None:
    fake = FakeScheduler()
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await open_screen(pilot)
        await pilot.press("s")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, SettingsScreen)
        assert not dialog.query("#timeout")  # cron: no daemon timings
        dialog.query_one("#timezone", Input).value = "Australia/Sydney"
        dialog.query_one("#package_schedules", Select).value = "auto"
        await pilot.click("#save")
        await settle(pilot)
        assert fake.made() == ["settings"]
        assert fake.calls[0].argv[-5:] == ["--timezone", "Australia/Sydney", "--package-schedules", "auto", "--json"]


async def test_add_runs_through_the_form_then_asks_when() -> None:
    fake = FakeScheduler()
    app = make_app(fake)
    async with app.run_test(size=SIZE) as pilot:
        await open_screen(pilot)
        await pilot.press("a")
        await pilot.pause()
        assert app.scheduling and not isinstance(app.screen, SchedulesScreen)
        deploy = next(r for r in app.runnables if r.name == "deploy")
        await app.open(deploy)
        await pilot.pause()
        assert "Schedule" in str(app.query_one("#run").label)
        app.query_one("#field-target #control").value = "prod"
        app.query_one("#field-release #control", Input).value = "v9"
        app.query_one("#field-token #control", Input).value = "s3cret"
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert not isinstance(app.screen, TimingScreen)  # a secret can't go in a schedule
        app.query_one("#field-token #control", Input).value = ""
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, TimingScreen)
        dialog = app.screen
        assert dialog.query_one("#sid", Input).value == "deploy"
        dialog.query_one("#value", Input).value = "06:00"
        await pilot.press("enter")
        await settle(pilot)
        assert fake.made() == ["add", "sync"]
        argv = fake.calls[0].argv
        assert argv[argv.index("--id") + 1] == "deploy" and argv[argv.index("--cron") + 1] == "0 6 * * *"
        assert json.loads(argv[argv.index("--args") + 1]) == {"target": "prod", "release": "v9"}
        assert not app.scheduling
        assert isinstance(app.screen, SchedulesScreen)


def test_now_fixture_is_utc() -> None:
    assert NOW.tzinfo is _dt.timezone.utc

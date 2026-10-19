from __future__ import annotations

import json

import pytest
from conftest import fixture_runnables
from textual.widgets import Collapsible, Input, OptionList, RichLog, Static, Switch

from runspec_tui import app as app_mod
from runspec_tui.app import RunspecTUI
from runspec_tui.fields import ArgField

SIZE = (140, 48)


class FakeRun:
    """Stands in for runner.Run: records the call, then plays back output."""

    calls: list[dict] = []
    output: list[tuple[str, str]] = []
    code = 0

    def __init__(self, runnable, argv, env, on_line, on_done, **_kw) -> None:
        FakeRun.calls.append({"runnable": runnable, "argv": argv, "env": env})
        self.on_line, self.on_done = on_line, on_done
        self.cancelled = False

    def start(self) -> None:
        import threading

        def play() -> None:
            for stream, line in FakeRun.output:
                self.on_line(stream, line)
            self.on_done(FakeRun.code, 0.1)

        threading.Thread(target=play, daemon=True).start()

    def cancel(self, grace: float = 5.0) -> None:
        self.cancelled = True


@pytest.fixture(autouse=True)
def fake_run(monkeypatch):
    FakeRun.calls, FakeRun.output, FakeRun.code = [], [], 0
    monkeypatch.setattr(app_mod, "Run", FakeRun)
    return FakeRun


def make_app() -> RunspecTUI:
    return RunspecTUI(fixture_runnables(), venv="/venv", user="ops")


def _option_ids(listing: OptionList) -> list[str | None]:
    return [listing.get_option_at_index(i).id for i in range(listing.option_count)]


async def test_lists_runnables_by_package_and_searches() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        listing = app.query_one("#runnables", OptionList)
        assert _option_ids(listing) == [None, "r:0", "r:1"]  # header, deploy, db
        await pilot.click("#search")
        await pilot.press(*"vacuum")
        assert _option_ids(listing) == [None, "r:1"]
        await pilot.press("enter")
        assert app.current is not None and app.current.name == "db"


async def test_form_sections_and_default_arguments() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await app.open(app.runnables[0])
        await pilot.pause()
        names = [f.arg["name"] for f in app.query(ArgField)]
        assert "target" in names and "workers" in names
        [collapsible] = app.query(Collapsible)
        assert collapsible.collapsed and "Default arguments (" in str(collapsible.title)
        assert app.query("#debug")  # the Debug switch is offered


async def test_required_fields_block_the_run_and_expand_their_section() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await app.open(app.runnables[0])
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert FakeRun.calls == []
        target = app.query_one("#field-target", ArgField)
        assert target.has_class("-invalid")
        assert "Required" in str(target.query_one(".arg-error", Static).render())


async def test_run_sends_argv_and_secrets_and_pretty_prints_json() -> None:
    FakeRun.output = [("stdout", json.dumps({"ok": True, "items": [1, 2]})), ("stderr", "a warning")]
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await app.open(app.runnables[0])
        await pilot.pause()
        app.query_one("#field-target #control").value = "uat"
        app.query_one("#field-release #control", Input).value = "v9"
        app.query_one("#field-token #control", Input).value = "s3cret"
        app.query_one("#debug", Switch).value = True
        await pilot.press("ctrl+r")
        await pilot.pause(0.3)
        [call] = FakeRun.calls
        assert call["argv"] == ["--target", "uat", "--release", "v9"]
        assert call["env"] == {"RUNSPEC_DEPLOY_ARG_TOKEN": "s3cret", "RUNSPEC_DEPLOY_ARG_DEBUG": "1"}
        log = app.query_one("#log", RichLog)
        text = " ".join(" ".join(strip.text for strip in log.lines).split())  # the log wraps long lines
        assert "RUNSPEC_DEPLOY_ARG_TOKEN=*** RUNSPEC_DEPLOY_ARG_DEBUG=1 deploy --target uat --release v9" in text
        assert '"ok": true' in text and "a warning" in text and "exit 0" in text
        assert "s3cret" not in text
        assert app.run_handle is None


async def test_commands_navigate_and_escape_goes_back() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await app.open(app.runnables[1])  # db requires a command
        await pilot.pause()
        assert not app.query("#run")
        commands = app.query_one("#commands", OptionList)
        assert _option_ids(commands) == ["backup", "vacuum"]
        commands.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.path == ("backup",)
        labels = [str(lbl.render()) for lbl in app.query(".section")]
        assert labels == ["INHERITED", "THIS COMMAND"]
        await pilot.press("escape")
        await pilot.pause()
        assert app.path == ()


async def test_a_value_from_the_environment_starts_blank_and_satisfies_required(monkeypatch, db) -> None:
    monkeypatch.setenv("RUNSPEC_DB_ARG_NAME", "orders")
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await app.open(app.runnables[1], ("backup",))
        await pilot.pause()
        field = app.query_one("#field-name", ArgField)
        assert field.source == "$RUNSPEC_DB_ARG_NAME" and field.value == ""
        await pilot.press("ctrl+r")
        await pilot.pause(0.3)
        [call] = FakeRun.calls
        assert call["argv"] == ["backup"]  # nothing typed: core reads the environment


async def test_focusing_a_field_shows_its_full_description() -> None:
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await app.open(app.runnables[0])
        await pilot.pause()
        app.query_one("#field-release", ArgField).focus_control()
        await pilot.pause()
        help_line = app.query_one("#help", Static)
        assert help_line.display and "Release tag" in str(help_line.render())


async def test_empty_venv_explains_itself() -> None:
    app = RunspecTUI([], venv="/venv", user="ops")
    async with app.run_test(size=SIZE):
        assert "No runnables here" in str(app.query_one("#welcome", Static).render())

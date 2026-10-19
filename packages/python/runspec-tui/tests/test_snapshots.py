"""Rendered-screen snapshots (pytest-textual-snapshot). After an intended visual
change, re-record with ``pytest tests/test_snapshots.py --snapshot-update`` and
review the SVGs in ``tests/__snapshots__``."""

from __future__ import annotations

from conftest import fixture_runnables

from runspec_tui.app import RunspecTUI

SIZE = (130, 44)


def make_app() -> RunspecTUI:
    return RunspecTUI(fixture_runnables(), venv="/opt/venvs/ops", user="ops")


def test_welcome(snap_compare) -> None:
    assert snap_compare(make_app(), terminal_size=SIZE)


def test_form(snap_compare) -> None:
    async def run_before(pilot) -> None:
        await pilot.app.open(pilot.app.runnables[0])
        await pilot.pause()

    assert snap_compare(make_app(), terminal_size=SIZE, run_before=run_before)


def test_command_picker(snap_compare) -> None:
    async def run_before(pilot) -> None:
        await pilot.app.open(pilot.app.runnables[1])
        await pilot.pause()

    assert snap_compare(make_app(), terminal_size=SIZE, run_before=run_before)


def test_validation_errors(snap_compare) -> None:
    async def run_before(pilot) -> None:
        await pilot.app.open(pilot.app.runnables[1], ("backup",))
        await pilot.pause()
        await pilot.press("ctrl+r")
        await pilot.pause()

    assert snap_compare(make_app(), terminal_size=SIZE, run_before=run_before)

"""Wiring / parsing tests for every runspec-o365 runnable — real spec, no mocks."""

from pathlib import Path

import pytest
from runspec.testing import ParseHarness

SPEC = Path(__file__).parent.parent / "runspec_o365" / "runspec.toml"

RUNNABLES = [
    "graph-login",
    "graph-whoami",
    "outlook-unread",
    "outlook-search",
    "outlook-folders",
    "calendar-upcoming",
    "calendar-today",
    "next-meeting",
    "onedrive-recent",
    "onedrive-search",
    "onedrive-list",
    "teams-list-chats",
    "teams-read-chat",
]

# A superset of every runnable's args; ParseHarness.smoke ignores keys a given
# runnable doesn't declare, so one dict drives all 13 spec smokes.
VALUES = {
    "query": "invoice",
    "chat-id": "19:abc@thread.v2",
    "count": 10,
    "days": 7,
    "path": "/Documents",
}


@pytest.mark.parametrize("name", RUNNABLES)
def test_spec_smoke(name: str) -> None:
    # --help exits 0, required args are enforced, and a populated call parses.
    h = ParseHarness(script_name=name, config_path=SPEC)
    h.smoke(values=VALUES)
    h.close()


def test_int_arg_coerces() -> None:
    h = ParseHarness(script_name="outlook-unread", config_path=SPEC)
    spec = h.expect_ok(["--count", "5"])
    assert spec.count.value == 5
    assert isinstance(spec.count.value, int)
    h.close()

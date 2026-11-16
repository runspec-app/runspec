"""
test_listed_args.py — a multi-valued arg recorded as text is sent as its values.

runspec core before 0.84.2 recorded every run arg with ``str()``, so a
``multiple`` choice picked as ``Open`` came back from History as ``"['Open']"``.
History's Rerun (and the agent re-running from ``search_history``) sent that
text as one value, which fails the runnable's choice check. The bridge turns it
back into the list before building argv.
"""

from __future__ import annotations

import pytest

from runspec_console import executor
from runspec_console.bridge_utils import _listed_text, repair_listed_args

from tests.test_agent_secret_split import _AgentBridge
from tests.test_ui_output_cap import _InvokeBridge


def _cached(host: str = "h") -> dict:
    return {
        "host": host,
        "name": "tickets",
        "group": None,
        "args": [
            {
                "name": "state",
                "type": "choice",
                "options": ["Open", "Closed", "Pending"],
                "multiple": True,
            },
            {"name": "level", "type": "choice", "options": ["low", "high"]},
            {"name": "note", "type": "str"},
            {"name": "files", "type": "rest"},
        ],
        "commands": {
            "close": {
                "name": "close",
                "args": [
                    {"name": "dry-run", "type": "flag"},
                    {"name": "tag", "type": "str", "multiple": True},
                ],
            }
        },
    }


@pytest.mark.parametrize(
    ("text", "items"),
    [
        ("['Open']", ["Open"]),
        ("['Open', 'Pending']", ["Open", "Pending"]),
        ('["Open"]', ["Open"]),
        ('["it\'s"]', ["it's"]),
        ("[1, 2]", ["1", "2"]),
        ("[]", []),
    ],
)
def test_listed_text_reads_lists(text, items):
    assert _listed_text(text) == items


@pytest.mark.parametrize(
    "text",
    [
        "Open",
        "[Open]",
        "['a'",
        "[['a']]",
        "[{'a': 1}]",
        "['a'] + ['b']",
        "[__import__('os')]",
    ],
)
def test_listed_text_rejects_everything_else(text):
    assert _listed_text(text) is None


def test_repair_turns_a_multiple_choice_back_into_its_picks():
    args = {"state": "['Open', 'Closed']", "files": "['a.txt', 'b.txt']"}
    repair_listed_args(_cached(), [], args)
    assert args == {"state": ["Open", "Closed"], "files": ["a.txt", "b.txt"]}


def test_repair_reads_the_subcommand_node():
    args = {"tag": "['x', 'y']", "state": "['Open']"}
    repair_listed_args(_cached(), ["close"], args)
    assert args == {"tag": ["x", "y"], "state": ["Open"]}


def test_repair_unwraps_a_single_choice_only_when_that_is_an_option():
    args = {"level": "['high']"}
    repair_listed_args(_cached(), [], args)
    assert args == {"level": "high"}
    args = {"level": "['nope']"}
    repair_listed_args(_cached(), [], args)
    assert args == {"level": "['nope']"}


def test_repair_leaves_everything_else_alone():
    args = {"note": "['Open']", "state": ["Open"], "level": "low", "unknown": "['x']"}
    copy = dict(args)
    repair_listed_args(_cached(), [], copy)
    assert copy == args
    # no discovery record: nothing is guessed
    bare = {"state": "['Open']"}
    repair_listed_args(None, [], bare)
    assert bare == {"state": "['Open']"}


def test_history_rerun_sends_the_picks_not_the_text(monkeypatch):
    bridge = _InvokeBridge()
    bridge._runnables_cache = [_cached("h")]
    captured: dict = {}

    def fake_run_local(rp, runnable, args, cp, on_line, on_done, **kw):
        captured["argv"] = executor.args_to_argv(args)
        on_done(0, 1)
        bridge._finish()

    monkeypatch.setattr(executor, "run_local", fake_run_local)
    bridge.invoke_runnable(
        "h", "tickets", {"state": "['Open', 'Pending']", "level": "low"}
    )
    assert bridge.done.wait(timeout=5)
    assert captured["argv"] == [
        "--state",
        "Open",
        "--state",
        "Pending",
        "--level",
        "low",
    ]


def test_agent_run_sends_the_picks_not_the_text(monkeypatch):
    captured: dict = {}

    def fake_run_local(rp, runnable, args, command_path, on_line, on_done, **kw):
        captured["args"] = args
        on_done(0, 1)

    monkeypatch.setattr(executor, "run_local", fake_run_local)
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda _e: ["/opt/app/runspec"]
    )
    b = _AgentBridge(entry={"name": "local"}, cached=_cached("local"))
    b._run_tool_sync("local__tickets", {"state": "['Open']"})
    assert captured["args"] == {"state": ["Open"]}

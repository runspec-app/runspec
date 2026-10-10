"""
test_debug_switch.py — the console-level ``debug`` switch.

Core auto-adds ``--debug`` to every runnable whose logging is on, but it isn't
in runspec.toml, so discovery never lists it. The console turns it on through the
``RUNSPEC_<RUNNABLE>_ARG_DEBUG`` env channel (never argv), so a runnable without
the flag (logging off, older runspec) ignores it instead of failing its run on an
unknown ``--debug``. A runnable that declares its own ``debug`` arg keeps it.
"""

from __future__ import annotations

import pytest

from runspec_console import executor
from runspec_console.bridge import Bridge
from runspec_console.bridge_utils import pop_debug

from tests.test_agent_secret_split import _AgentBridge


def _cached(host: str, *, own_debug: bool = False) -> dict:
    args = [{"name": "path", "type": "str"}]
    if own_debug:
        args.append({"name": "debug", "type": "choice", "options": ["a", "b"]})
    return {
        "host": host,
        "name": "disk-check",
        "group": None,
        "args": args,
        "commands": {
            "scan": {"name": "scan", "args": [{"name": "depth", "type": "int"}]}
        },
    }


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    def fake_run_local(rp, runnable, args, command_path, on_line, on_done, **kw):
        captured["args"] = args
        captured["cred_env"] = kw.get("cred_env")
        captured["command_path"] = command_path
        on_done(0, 1)

    monkeypatch.setattr("runspec_console.executor.run_local", fake_run_local)
    monkeypatch.setattr(
        "runspec_console.bridge_utils._paths", lambda _e: ["/opt/app/runspec"]
    )
    return captured


def test_debug_env_name():
    assert executor.debug_env("disk-check") == {"RUNSPEC_DISK_CHECK_ARG_DEBUG": "1"}


def test_pop_debug_rules():
    pop = pop_debug
    on = {"RUNSPEC_DISK_CHECK_ARG_DEBUG": "1"}
    args = {"path": "/", "debug": True}
    assert pop(_cached("local"), [], args) == on
    assert args == {"path": "/"}
    # off / falsy strings are removed but set nothing
    for value in (False, "false", "0", ""):
        args = {"debug": value}
        assert pop(_cached("local"), [], args) == {}
        assert args == {}
    assert pop(_cached("local"), [], {"debug": "true"}) == on
    # the runnable's own debug arg is a plain argument
    args = {"debug": "a"}
    assert pop(_cached("local", own_debug=True), [], args) == {}
    assert args == {"debug": "a"}
    # no discovery record: leave the call exactly as it was
    args = {"debug": True}
    assert pop(None, [], args) == {}
    assert args == {"debug": True}


def test_dedup_call_debug_rides_env_not_argv(monkeypatch):
    captured = _capture(monkeypatch)
    b = _AgentBridge(entry={"name": "local"}, cached=_cached("local"))
    b._run_tool_sync("local__disk-check", {"path": "/", "debug": True})
    assert captured["args"] == {"path": "/"}
    assert captured["cred_env"]["RUNSPEC_DISK_CHECK_ARG_DEBUG"] == "1"


def test_dedup_call_without_debug_is_unchanged(monkeypatch):
    captured = _capture(monkeypatch)
    b = _AgentBridge(entry={"name": "local"}, cached=_cached("local"))
    b._run_tool_sync("local__disk-check", {"path": "/"})
    assert captured["args"] == {"path": "/"}
    assert "RUNSPEC_DISK_CHECK_ARG_DEBUG" not in (captured["cred_env"] or {})


@pytest.mark.parametrize(
    "call",
    [
        {"debug": True, "args": {"depth": 2}},  # top-level (the documented spot)
        {"args": {"depth": 2, "debug": True}},  # misplaced inside args
    ],
)
def test_run_runnable_debug(monkeypatch, call):
    captured = _capture(monkeypatch)
    b = _AgentBridge(entry={"name": "local"}, cached=_cached("local"))
    out = b._run_tool_sync(
        "run_runnable",
        {"id": "local__disk-check", "host": "local", "command": "scan", **call},
    )
    assert not out.startswith("Error"), out
    assert captured["command_path"] == ["scan"]
    assert captured["args"] == {"depth": 2}
    assert captured["cred_env"]["RUNSPEC_DISK_CHECK_ARG_DEBUG"] == "1"


def test_own_debug_arg_is_passed_through(monkeypatch):
    captured = _capture(monkeypatch)
    b = _AgentBridge(entry={"name": "local"}, cached=_cached("local", own_debug=True))
    b._run_tool_sync("local__disk-check", {"debug": "b"})
    assert captured["args"] == {"debug": "b"}
    assert "RUNSPEC_DISK_CHECK_ARG_DEBUG" not in (captured["cred_env"] or {})


def test_debug_is_a_top_level_run_runnable_field():
    assert "debug" in Bridge._RUN_RUNNABLE_FIELDS
    assert Bridge._misplaced_run_fields({"id": "x", "host": "y", "debug": True}) is None


def test_run_runnable_top_level_debug_leaves_own_debug_arg_alone(monkeypatch):
    captured = _capture(monkeypatch)
    b = _AgentBridge(entry={"name": "local"}, cached=_cached("local", own_debug=True))
    out = b._run_tool_sync(
        "run_runnable",
        {
            "id": "local__disk-check",
            "host": "local",
            "debug": True,
            "args": {"debug": "a"},
        },
    )
    assert not out.startswith("Error"), out
    assert captured["args"] == {"debug": "a"}
    assert "RUNSPEC_DISK_CHECK_ARG_DEBUG" not in (captured["cred_env"] or {})

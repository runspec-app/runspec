"""
test_mcp_server_gate.py — the Bridge-side gate logic for the console MCP server
(_mcp_server_execute): manual refused, autonomous runs, confirm approved/edited/
timed-out/declined. Uses a light fake with the gate primitives stubbed — no SDK,
no socket, no real executor.
"""

from __future__ import annotations

import threading

from runspec_console.bridge_mcp_server import McpServerMixin
from runspec_console.mcp_server import ExecTool


def _tool(name="greet", autonomy="autonomous", command_path=None):
    return ExecTool(
        name=name,
        description="",
        input_schema={"type": "object", "properties": {}},
        host="local",
        group="venv",
        runnable=name,
        command_path=command_path or [],
        autonomy=autonomy,
    )


class FakeBridge(McpServerMixin):
    """McpServerMixin + just the gate/exec primitives it calls."""

    def __init__(self, *, approve=True, edited=None, cancelled=False, answer=True):
        self._approve = approve
        self._edited = edited
        self._cancelled = cancelled
        self._answer = answer  # whether the confirm "event" is set (answered)
        self.runs: list[tuple] = []
        self.registered: list[tuple] = []

    # gate primitives ------------------------------------------------------
    def _confirm_timeout_s(self, headless=False):
        return 300.0

    def _register_confirm(self, chat_id, tool_name, tool_input, level):
        self.registered.append((chat_id, tool_name, dict(tool_input), level))
        event = threading.Event()
        if self._answer:
            event.set()  # operator answered before the wait
        holder = {
            "approved": self._approve,
            "cancelled": self._cancelled,
            "edited_input": self._edited,
        }
        return "req-abc123", event, holder

    def _finish_confirm(self, request_id, holder, timed_out):
        edited = (
            holder.get("edited_input")
            if (holder["approved"] and not timed_out)
            else None
        )
        approved = holder["approved"] and not timed_out
        return approved, edited

    # executor -------------------------------------------------------------
    def invoke_runnable(
        self,
        host,
        runnable,
        args,
        *,
        command_path=None,
        group=None,
        trigger="",
        on_progress=None,
        **kw,
    ):
        self.runs.append(
            (host, runnable, dict(args), tuple(command_path or []), trigger)
        )
        # simulate a fire-and-stream run that finishes immediately
        if on_progress:
            on_progress("line", {"line": f"ran {runnable} {args}"})
            on_progress("done", {"exit_code": 0, "duration_ms": 5})
        return "inv-1"


def test_manual_is_refused_without_running():
    b = FakeBridge()
    text, is_error = b._mcp_server_execute(_tool("danger", autonomy="manual"), {})
    assert is_error is True
    assert "manual" in text
    assert b.runs == []  # never executed


def test_autonomous_runs_and_returns_output():
    b = FakeBridge()
    text, is_error = b._mcp_server_execute(
        _tool("greet", autonomy="autonomous"), {"who": "x"}
    )
    assert is_error is False
    assert "ran greet" in text
    assert b.registered == []  # autonomous → no confirm raised
    assert b.runs and b.runs[0][1] == "greet" and b.runs[0][4] == "mcp"


def test_confirm_approved_runs():
    b = FakeBridge(approve=True, answer=True)
    text, is_error = b._mcp_server_execute(_tool(autonomy="confirm"), {"a": 1})
    assert is_error is False
    assert b.registered and b.registered[0][3] == "confirm"
    assert b.runs  # ran after approval


def test_confirm_edited_input_is_used():
    b = FakeBridge(approve=True, answer=True, edited={"a": 999})
    b._mcp_server_execute(_tool(autonomy="confirm"), {"a": 1})
    assert b.runs[0][2] == {"a": 999}  # edited args won


def test_confirm_declined_does_not_run():
    b = FakeBridge(approve=False, answer=True)
    text, is_error = b._mcp_server_execute(_tool(autonomy="confirm"), {})
    assert is_error is True
    assert "declined" in text.lower()
    assert b.runs == []


def test_confirm_timeout_returns_resumable_and_does_not_run():
    # answer=False → the event is never set → the bounded wait elapses.
    b = FakeBridge(answer=False)
    # shrink the bound so the test doesn't actually wait 300s
    b._mcp_server_confirm_wait = lambda: 0.05  # type: ignore
    text, is_error = b._mcp_server_execute(_tool(autonomy="confirm"), {})
    assert is_error is False  # resumable, not an error
    assert "awaiting operator approval" in text.lower()
    assert b.runs == []  # nothing ran (no orphan execution)

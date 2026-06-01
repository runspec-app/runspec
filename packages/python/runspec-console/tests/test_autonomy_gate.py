"""
test_autonomy_gate.py — the agent (chat) path must honour runnable autonomy.

Binds the real gate methods onto a light stub (no Bridge.__init__ side effects).
_run_tool_sync and _dispatch are stubbed so nothing actually executes or touches
a UI; we assert on the gate decision and the confirm round-trip.
"""

from __future__ import annotations

import threading
import time

from runspec_console.bridge import Bridge


class _StubBridge:
    def __init__(self, runnables, run_result="RAN"):
        self._lock = threading.Lock()
        self._runnables_cache = runnables
        self._pending_confirms = {}
        self._dispatched = []
        self._run_result = run_result

    def _dispatch(self, event, detail):
        self._dispatched.append((event, detail))

    def _run_tool_sync(self, name, tool_input):
        return self._run_result

    _effective_tool_autonomy = Bridge._effective_tool_autonomy
    _gated_run_tool = Bridge._gated_run_tool
    _await_confirmation = Bridge._await_confirmation
    resolve_tool_confirmation = Bridge.resolve_tool_confirmation


def _runnable(autonomy="confirm", args=None):
    return [
        {"host": "local", "name": "flush-dns", "autonomy": autonomy, "args": args or []}
    ]


# ── effective autonomy ────────────────────────────────────────────────────────


def test_effective_autonomy_base_level():
    b = _StubBridge(_runnable("confirm"))
    assert b._effective_tool_autonomy("local__flush-dns", {}) == "confirm"


def test_effective_autonomy_unknown_runnable_defaults_confirm():
    b = _StubBridge([])
    assert b._effective_tool_autonomy("local__nope", {}) == "confirm"


def test_effective_autonomy_arg_escalates():
    args = [{"name": "force", "autonomy": "manual"}]
    b = _StubBridge(_runnable("confirm", args))
    # arg not provided → base level
    assert b._effective_tool_autonomy("local__flush-dns", {}) == "confirm"
    # arg provided → escalates to the more restrictive per-arg level
    assert b._effective_tool_autonomy("local__flush-dns", {"force": True}) == "manual"


# ── gate decisions ────────────────────────────────────────────────────────────


def test_autonomous_runs_without_prompt():
    b = _StubBridge(_runnable("autonomous"), run_result="RAN")
    out = b._gated_run_tool("local__flush-dns", {}, "chat1")
    assert out == "RAN"
    assert b._dispatched == []  # no confirmation event


def test_manual_is_hard_blocked():
    b = _StubBridge(_runnable("manual"), run_result="RAN")
    out = b._gated_run_tool("local__flush-dns", {}, "chat1")
    assert "manual-only" in out
    assert out != "RAN"  # never executed
    assert b._dispatched == []  # not even a prompt — hard block


def _drive(b, tool_input, decision):
    """Run _gated_run_tool in a thread, answer the confirmation with `decision`."""
    result = {}
    t = threading.Thread(
        target=lambda: result.__setitem__(
            "out", b._gated_run_tool("local__flush-dns", tool_input, "chat1")
        )
    )
    t.start()
    for _ in range(400):  # wait for the confirm event to be dispatched
        if b._dispatched:
            break
        time.sleep(0.005)
    assert b._dispatched, "expected a tool_confirm dispatch"
    event, detail = b._dispatched[0]
    assert event == "runspec:tool_confirm"
    b.resolve_tool_confirmation(detail["request_id"], decision)
    t.join(timeout=5)
    return result["out"], detail


def test_confirm_approve_runs():
    b = _StubBridge(_runnable("confirm"), run_result="RAN")
    out, detail = _drive(b, {}, True)
    assert out == "RAN"
    assert detail["autonomy"] == "confirm"


def test_confirm_deny_does_not_run():
    b = _StubBridge(_runnable("confirm"), run_result="RAN")
    out, _ = _drive(b, {}, False)
    assert "declined" in out
    assert out != "RAN"


def test_supervised_also_prompts():
    b = _StubBridge(_runnable("supervised"), run_result="RAN")
    out, detail = _drive(b, {}, True)
    assert out == "RAN"
    assert detail["autonomy"] == "supervised"


def test_resolve_unknown_id_is_noop():
    b = _StubBridge(_runnable())
    b.resolve_tool_confirmation("does-not-exist", True)  # must not raise

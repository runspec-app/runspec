"""
test_ui_output_cap.py — a direct Console run must not flood the UI.

``invoke_runnable`` streams a runnable's stdout to the console card by dispatching
one ``runspec:output`` event per line. A runnable that dumps a huge blob (e.g. 10k
lines of pretty-printed JSON) otherwise fires thousands of events — O(n) IPC plus
O(n²) React reconciliation — hanging the card for minutes. These tests drive the
real method with a fake executor and assert the bridge stops dispatching past the
line/byte cap and emits a single sentinel, and that a normal run passes through
whole.
"""

from __future__ import annotations

import threading

from runspec_console.bridge import Bridge
from runspec_console.bridge_const import _UI_OUTPUT_LINE_CAP


class _InvokeBridge:
    invoke_runnable = Bridge.invoke_runnable
    _ui_output_caps = Bridge._ui_output_caps
    _positionals_for = staticmethod(Bridge._positionals_for)

    def __init__(self, config: dict | None = None):
        self._entry = {"runspec_paths": ["/opt/venv/bin/runspec"]}
        self._runnables_cache = [
            {"host": "h", "name": "dump", "group": None, "args": []}
        ]
        self._lock = threading.Lock()
        self._in_flight: dict = {}
        self._config = config or {}
        self.dispatched: list[tuple[str, dict]] = []
        self.done = threading.Event()

    # ── dependencies invoke_runnable reaches for (stubbed to no-ops) ──
    def _host_entry(self, _host):
        return self._entry

    def get_config(self):
        return self._config

    def _become_params(self, _cached, _host):
        return "", "sudo", None

    def _secret_arg_names(self, _cached, _cp):
        return set()

    def _credential_env(self, _host, _runnable, _ids):
        return {}, []

    def _apply_selected_credentials(self, *_a, **_k):
        return []

    def _declared_arg_names(self, _cached, _cp):
        return set()

    def _current_user(self):
        return "tester"

    def _play_sound(self, _name):
        pass

    def _dispatch(self, event, detail):
        self.dispatched.append((event, detail))

    # capture completion so tests can join the worker thread deterministically
    def _finish(self):
        self.done.set()


def _run(bridge: _InvokeBridge, monkeypatch, n_lines: int, line: str = "x"):
    """Patch the local executor to emit ``n_lines`` stdout lines, then run."""
    from runspec_console import executor as executor_mod

    def fake_run_local(rp, runnable, args, cp, on_line, on_done, **kw):
        for _ in range(n_lines):
            on_line(line, "stdout")
        on_done(0, 1)
        bridge._finish()

    monkeypatch.setattr(executor_mod, "run_local", fake_run_local)
    bridge.invoke_runnable("h", "dump", {})
    assert bridge.done.wait(timeout=5), "worker thread did not finish"


def _output_lines(bridge: _InvokeBridge) -> list[dict]:
    return [d for e, d in bridge.dispatched if e == "runspec:output"]


def test_small_run_passes_through_unchanged(monkeypatch):
    b = _InvokeBridge()
    _run(b, monkeypatch, n_lines=50)
    lines = _output_lines(b)
    assert len(lines) == 50
    assert all("truncated" not in ln["line"].lower() for ln in lines)


def test_over_cap_run_stops_and_emits_one_sentinel(monkeypatch):
    b = _InvokeBridge()
    _run(b, monkeypatch, n_lines=_UI_OUTPUT_LINE_CAP + 500)
    lines = _output_lines(b)
    # Capped at the line limit + exactly one sentinel, not one event per line.
    assert len(lines) == _UI_OUTPUT_LINE_CAP + 1
    sentinels = [ln for ln in lines if "truncated in the console" in ln["line"]]
    assert len(sentinels) == 1
    assert sentinels[0]["stream"] == "stderr"
    assert "lines" in sentinels[0]["line"]


def test_byte_cap_trips_before_line_cap(monkeypatch):
    # A few very long lines exceed the byte cap well under the line cap.
    b = _InvokeBridge({"console": {"output_byte_cap": 10_000}})
    _run(b, monkeypatch, n_lines=100, line="y" * 4_000)
    lines = _output_lines(b)
    sentinels = [ln for ln in lines if "truncated in the console" in ln["line"]]
    assert len(sentinels) == 1
    assert "bytes" in sentinels[0]["line"]
    # Stopped early: far fewer than the 100 emitted lines got through.
    assert len(lines) < 10


def test_zero_cap_disables_the_limit(monkeypatch):
    b = _InvokeBridge({"console": {"output_line_cap": 0, "output_byte_cap": 0}})
    _run(b, monkeypatch, n_lines=_UI_OUTPUT_LINE_CAP + 200)
    lines = _output_lines(b)
    assert len(lines) == _UI_OUTPUT_LINE_CAP + 200
    assert all("truncated" not in ln["line"].lower() for ln in lines)

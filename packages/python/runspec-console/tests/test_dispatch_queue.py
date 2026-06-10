"""
test_dispatch_queue.py — UI dispatches are serialised through one thread.

All `_dispatch` calls go onto a FIFO queue drained by a single thread, so
evaluate_js is never called concurrently from many threads (trigger workers, the
COM event pump, the agent loop). Concurrent background-thread evaluate_js was
racy on pywebview — under a burst the autonomy-confirm prompt could be lost.
The loop is bound onto a bare stub (no Bridge.__init__ side effects).
"""

from __future__ import annotations

import queue
import threading
import types

from runspec_console.bridge import Bridge


class _Window:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def evaluate_js(self, js: str) -> None:
        self.calls.append(js)


def _stub(window) -> types.SimpleNamespace:
    s = types.SimpleNamespace()
    s._window = window
    s._dispatch_q = queue.Queue()
    s._dispatch = types.MethodType(Bridge._dispatch, s)
    s._ui_dispatch_loop = types.MethodType(Bridge._ui_dispatch_loop, s)
    return s


def test_dispatches_deliver_in_order():
    w = _Window()
    s = _stub(w)
    t = threading.Thread(target=s._ui_dispatch_loop, daemon=True)
    t.start()
    s._dispatch("runspec:a", {"n": 1})
    s._dispatch("runspec:b", {"n": 2})
    s._dispatch_q.put(None)  # stop sentinel
    t.join(timeout=2)
    assert not t.is_alive()
    assert len(w.calls) == 2
    assert '"runspec:a"' in w.calls[0]
    assert '"runspec:b"' in w.calls[1]


def test_skips_when_no_window_without_crashing():
    s = _stub(None)  # no window attached
    t = threading.Thread(target=s._ui_dispatch_loop, daemon=True)
    t.start()
    s._dispatch("runspec:a", {"n": 1})  # must not raise
    s._dispatch_q.put(None)
    t.join(timeout=2)
    assert not t.is_alive()


def test_one_bad_dispatch_does_not_kill_the_loop():
    class _Boom(_Window):
        def evaluate_js(self, js: str) -> None:
            if "boom" in js:
                raise RuntimeError("evaluate failed")
            self.calls.append(js)

    w = _Boom()
    s = _stub(w)
    t = threading.Thread(target=s._ui_dispatch_loop, daemon=True)
    t.start()
    s._dispatch("runspec:boom", {})  # raises inside the loop, must be swallowed
    s._dispatch("runspec:ok", {})  # still delivered
    s._dispatch_q.put(None)
    t.join(timeout=2)
    assert not t.is_alive()
    assert any('"runspec:ok"' in c for c in w.calls)

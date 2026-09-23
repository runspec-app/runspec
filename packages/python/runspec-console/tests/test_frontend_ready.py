"""
Startup-reliability fixes:

* the asset server must be a *concurrent* ThreadingHTTPServer (not the
  single-threaded HTTPServer), so parallel asset fetches on a cold launch don't
  head-of-line-block each other — the starvation that could abort a fetch and
  blank the window; and
* the deferred-bring-up handshake (``bridge.notify_frontend_ready``) must forward
  to app.main's trigger and never raise into the UI's fire-and-forget call.
"""

from __future__ import annotations

import runspec_console.app as app
from runspec_console.bridge_infra import InfraMixin


def test_static_server_is_threaded_and_daemonized(tmp_path, monkeypatch):
    captured: dict = {}

    class FakeServer:
        def __init__(self, addr, handler):
            captured["addr"] = addr
            captured["server"] = self
            self.daemon_threads = False

        def serve_forever(self):  # runs on the daemon thread; no-op for the test
            captured["served"] = True

    monkeypatch.setattr(app, "ThreadingHTTPServer", FakeServer)
    port = app._start_static_server(tmp_path)

    assert isinstance(port, int) and port > 0
    # Concurrent server, daemonized workers (a client holding a connection open can
    # never delay process exit).
    assert captured["server"].daemon_threads is True
    assert captured["addr"][0] == "127.0.0.1"
    # The single-threaded HTTPServer import is gone — it can't be used by accident.
    assert not hasattr(app, "HTTPServer")


class _Infra(InfraMixin):
    """Bare InfraMixin instance — the frontend-ready methods only touch
    ``self._on_frontend_ready`` and the logger, so they test in isolation."""


def test_notify_frontend_ready_forwards_and_tolerates_unset():
    infra = _Infra()
    # Called before app.main wires the callback → a no-op, never raises.
    infra.notify_frontend_ready()

    calls: list[int] = []
    infra._on_frontend_ready = lambda: calls.append(1)
    infra.notify_frontend_ready()
    infra.notify_frontend_ready()
    # The bridge method forwards every call; the *once*-guard lives in app.main's
    # trigger (so the UI signal, the window 'shown' backstop, and the fallback
    # Timer can all funnel through here safely).
    assert calls == [1, 1]


def test_notify_frontend_ready_swallows_a_raising_callback():
    infra = _Infra()

    def boom():
        raise RuntimeError("bring-up blew up")

    infra._on_frontend_ready = boom
    # A backstop must never wedge the caller.
    infra.notify_frontend_ready()

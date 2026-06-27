"""
test_ssh_logging.py — paramiko log capture into the Dev tab.

The Bridge installs a logging handler on the ``paramiko`` logger that forwards
records as ``runspec:ssh`` events (the Dev-tab investigation aid for
"Error reading SSH protocol banner") and keeps them off the launching terminal.
These tests exercise the handler and the dispatch guard in isolation — no
pywebview window is created, so they run on any platform.
"""

from __future__ import annotations

import logging
import queue
import threading

from runspec_console.bridge import Bridge
from runspec_console.bridge_helpers import _ParamikoDevHandler


class _FakeBridge:
    """Records what the handler forwards, standing in for a real Bridge."""

    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def _emit_ssh_log(self, detail: dict) -> None:
        self.emitted.append(detail)


def _record(
    level: int, msg: str, exc_info=None, name="paramiko.transport"
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )


def test_handler_forwards_level_and_message() -> None:
    fb = _FakeBridge()
    handler = _ParamikoDevHandler(fb)  # type: ignore[arg-type]
    handler.emit(_record(logging.ERROR, "Error reading SSH protocol banner"))
    assert len(fb.emitted) == 1
    detail = fb.emitted[0]
    assert detail["level"] == "ERROR"
    assert detail["logger"] == "paramiko.transport"
    assert detail["message"] == "Error reading SSH protocol banner"
    assert "traceback" not in detail


def test_handler_attributes_host_from_tagged_channel() -> None:
    """A per-transport channel (paramiko.transport.<host>) surfaces the host on
    the event and prefixes the message — so an idle-disconnect names its host."""
    fb = _FakeBridge()
    handler = _ParamikoDevHandler(fb)  # type: ignore[arg-type]
    handler.emit(
        _record(
            logging.ERROR, "EOF during negotiation", name="paramiko.transport.web01"
        )
    )
    detail = fb.emitted[0]
    assert detail["host"] == "web01"
    assert detail["message"] == "[web01] EOF during negotiation"


def test_handler_strips_sftp_suffix_from_host() -> None:
    fb = _FakeBridge()
    handler = _ParamikoDevHandler(fb)  # type: ignore[arg-type]
    handler.emit(_record(logging.INFO, "sftp open", name="paramiko.transport.db1.sftp"))
    assert fb.emitted[0]["host"] == "db1"


def test_handler_leaves_untagged_records_unchanged() -> None:
    """An untagged record (the shared channel / connect-time banner error before
    a transport exists) carries no host and its message is verbatim."""
    fb = _FakeBridge()
    handler = _ParamikoDevHandler(fb)  # type: ignore[arg-type]
    handler.emit(_record(logging.ERROR, "Error reading SSH protocol banner"))
    detail = fb.emitted[0]
    assert "host" not in detail
    assert detail["message"] == "Error reading SSH protocol banner"


def test_handler_includes_traceback_on_exc_info() -> None:
    fb = _FakeBridge()
    handler = _ParamikoDevHandler(fb)  # type: ignore[arg-type]
    try:
        raise TimeoutError("socket timed out")
    except TimeoutError:
        import sys

        handler.emit(_record(logging.ERROR, "banner", exc_info=sys.exc_info()))
    detail = fb.emitted[0]
    assert "traceback" in detail
    assert "TimeoutError" in detail["traceback"]


def test_handler_never_raises_when_emit_fails() -> None:
    class _Boom:
        def _emit_ssh_log(self, detail: dict) -> None:
            raise RuntimeError("dispatch exploded")

    handler = _ParamikoDevHandler(_Boom())  # type: ignore[arg-type]
    handler.emit(_record(logging.INFO, "anything"))  # must not raise


# ── _emit_ssh_log dispatch guard (bound onto a stub, no Bridge.__init__) ───────


class _Stub:
    _emit_ssh_log = Bridge._emit_ssh_log
    _dispatch = Bridge._dispatch
    _ui_dispatch_loop = Bridge._ui_dispatch_loop

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pre_window_ssh_logs: list[dict] = []
        self._dispatch_q: queue.Queue = queue.Queue()


def test_emit_ssh_log_buffers_without_window() -> None:
    """Before the window attaches, records are buffered (not dropped) so the
    first refresh cycle's connect logs survive to be flushed later."""
    s = _Stub()
    s._window = None
    s._emit_ssh_log({"level": "ERROR", "message": "x"})  # must not raise/dispatch
    assert s._pre_window_ssh_logs == [{"level": "ERROR", "message": "x"}]


def test_emit_ssh_log_buffer_is_bounded() -> None:
    s = _Stub()
    s._window = None
    for i in range(250):
        s._emit_ssh_log({"i": i})
    assert len(s._pre_window_ssh_logs) == 200  # capped, no unbounded growth


def test_emit_ssh_log_dispatches_with_window() -> None:
    sent: list[tuple[str, dict]] = []

    class _Win:
        def evaluate_js(self, js: str) -> None:
            sent.append(("js", {"js": js}))

    s = _Stub()
    s._window = _Win()
    # Dispatch is async now (serialised through the ui-dispatch loop); drive it.
    t = threading.Thread(target=s._ui_dispatch_loop, daemon=True)
    t.start()
    s._emit_ssh_log({"level": "ERROR", "message": "boom"})
    s._dispatch_q.put(None)
    t.join(timeout=2)
    assert len(sent) == 1
    assert "runspec:ssh" in sent[0][1]["js"]

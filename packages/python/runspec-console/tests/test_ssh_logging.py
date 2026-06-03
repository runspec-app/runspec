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

from runspec_console.bridge import Bridge, _ParamikoDevHandler


class _FakeBridge:
    """Records what the handler forwards, standing in for a real Bridge."""

    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def _emit_ssh_log(self, detail: dict) -> None:
        self.emitted.append(detail)


def _record(level: int, msg: str, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="paramiko.transport",
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


def test_emit_ssh_log_noop_without_window() -> None:
    s = _Stub()
    s._window = None
    s._emit_ssh_log({"level": "ERROR", "message": "x"})  # must not raise/dispatch


def test_emit_ssh_log_dispatches_with_window() -> None:
    sent: list[tuple[str, dict]] = []

    class _Win:
        def evaluate_js(self, js: str) -> None:
            sent.append(("js", {"js": js}))

    s = _Stub()
    s._window = _Win()
    s._emit_ssh_log({"level": "ERROR", "message": "boom"})
    assert len(sent) == 1
    assert "runspec:ssh" in sent[0][1]["js"]

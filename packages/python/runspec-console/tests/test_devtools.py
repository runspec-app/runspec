"""
test_devtools.py — Dev tab debug-state plumbing.

Covers app._resolve_debug (the --dev / --devtools / env precedence) and the
Bridge.set_debug / is_debug_enabled / open_devtools helpers the Dev tab calls.
Pure logic — no pywebview window is created, so this runs on any platform.
"""

from __future__ import annotations

from runspec_console.app import _resolve_debug
from runspec_console.bridge import Bridge


# ── _resolve_debug ────────────────────────────────────────────────────────────


def test_dev_implies_debug() -> None:
    assert _resolve_debug(dev=True, devtools_arg=False) is True


def test_devtools_flag_enables_debug() -> None:
    assert _resolve_debug(dev=False, devtools_arg=True) is True


def test_env_var_enables_debug(monkeypatch) -> None:
    monkeypatch.setenv("RUNSPEC_CONSOLE_DEVTOOLS", "1")
    assert _resolve_debug(dev=False, devtools_arg=False) is True


def test_env_var_other_value_does_not_enable(monkeypatch) -> None:
    monkeypatch.setenv("RUNSPEC_CONSOLE_DEVTOOLS", "0")
    assert _resolve_debug(dev=False, devtools_arg=False) is False


def test_default_is_off(monkeypatch) -> None:
    monkeypatch.delenv("RUNSPEC_CONSOLE_DEVTOOLS", raising=False)
    assert _resolve_debug(dev=False, devtools_arg=False) is False


# ── Bridge debug state (bind methods onto a light stub, no __init__) ───────────


class _Stub:
    set_debug = Bridge.set_debug
    is_debug_enabled = Bridge.is_debug_enabled
    open_devtools = Bridge.open_devtools


def test_is_debug_enabled_defaults_false() -> None:
    s = _Stub()
    s._debug = False
    assert s.is_debug_enabled() is False


def test_set_debug_roundtrip() -> None:
    s = _Stub()
    s.set_debug(True)
    assert s.is_debug_enabled() is True
    s.set_debug(False)
    assert s.is_debug_enabled() is False


def test_open_devtools_no_window_is_noop() -> None:
    s = _Stub()
    s._window = None
    s.open_devtools()  # must not raise


def test_open_devtools_invokes_window_hook() -> None:
    calls = []

    class _Win:
        def open_devtools(self) -> None:
            calls.append("opened")

    s = _Stub()
    s._window = _Win()
    s.open_devtools()
    assert calls == ["opened"]


def test_open_devtools_swallows_hook_errors() -> None:
    class _Win:
        def open_devtools(self) -> None:
            raise RuntimeError("backend has no devtools")

    s = _Stub()
    s._window = _Win()
    s.open_devtools()  # must not raise

"""The shared no-window subprocess flags (procutil.no_window_kwargs).

Windows spawns a fresh console for a console child of a windowed (pythonw)
parent; CREATE_NO_WINDOW suppresses that flash. The helper is a no-op off
Windows so every local spawn can splat it unconditionally.
"""

from __future__ import annotations

import subprocess

import runspec_console.procutil as procutil


def test_no_window_kwargs_off_windows(monkeypatch):
    # No CREATE_NO_WINDOW attribute exists off Windows → empty, splat is a no-op.
    monkeypatch.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)
    assert procutil.no_window_kwargs() == {}


def test_no_window_kwargs_on_windows(monkeypatch):
    # Simulate the Windows attribute being present.
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert procutil.no_window_kwargs() == {"creationflags": 0x08000000}


def test_no_window_kwargs_is_splattable():
    # Whatever the platform, the result is a dict of subprocess kwargs.
    kwargs = procutil.no_window_kwargs()
    assert isinstance(kwargs, dict)
    assert set(kwargs) <= {"creationflags"}

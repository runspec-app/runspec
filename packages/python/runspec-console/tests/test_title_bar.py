"""The native title-bar caption waits for the window instead of a fixed guess.

pywebview runs the ``started`` hook on a thread it launches *before* building
the native form, so ``_apply_dwm_title_bar`` must poll for the HWND; a single
150 ms ``FindWindow`` used to miss on slower launches and silently leave the
default caption (the white-label ``title_bar_color`` never applied).
"""

from __future__ import annotations

import ctypes
import sys
import types

from runspec_console import app


class _Dwm:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def DwmSetWindowAttribute(self, hwnd, attr, ptr, size):  # noqa: N802
        self.calls.append((hwnd, attr))
        return 0


def _install(monkeypatch, find_results):
    results = iter(find_results)
    finds: list[str] = []

    def find_window(_cls, title):
        finds.append(title)
        return next(results, 0)

    monkeypatch.setitem(
        sys.modules, "win32gui", types.SimpleNamespace(FindWindow=find_window)
    )
    dwm = _Dwm()
    monkeypatch.setattr(
        ctypes, "windll", types.SimpleNamespace(dwmapi=dwm), raising=False
    )
    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr(
        "runspec_console.config.title_bar_colors", lambda: (0x000000FF, 0x00FFFFFF)
    )
    return finds, dwm


def test_polls_until_window_exists(monkeypatch):
    finds, dwm = _install(monkeypatch, [0, 0, 0, 4242])
    app._apply_dwm_title_bar("Mycorp Console")
    assert len(finds) == 4
    assert {attr for _h, attr in dwm.calls} == {20, 34, 35, 36}
    assert all(h == 4242 for h, _a in dwm.calls)


def test_gives_up_quietly_after_deadline(monkeypatch):
    finds, dwm = _install(monkeypatch, [])
    monkeypatch.setattr(app, "_TITLE_BAR_WAIT_S", 0.0)
    app._apply_dwm_title_bar("Mycorp Console")
    assert len(finds) == 1
    assert dwm.calls == []

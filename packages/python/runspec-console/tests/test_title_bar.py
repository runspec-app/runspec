"""The native title-bar caption: only when configured, and before first paint.

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
    """Records each attribute set; ``refuse`` = attribute ids that fail."""

    def __init__(self, refuse: frozenset[int] = frozenset()) -> None:
        self.calls: list[tuple[int, int]] = []
        self.values: dict[int, int] = {}
        self.refuse = refuse

    def DwmSetWindowAttribute(self, hwnd, attr, ptr, size):  # noqa: N802
        self.calls.append((hwnd, attr))
        if attr in self.refuse:
            return -2147024809  # E_INVALIDARG, as on Windows 10
        self.values[attr] = ctypes.cast(
            ptr, ctypes.POINTER(ctypes.c_int)
        ).contents.value
        return 0


def _install(
    monkeypatch, find_results, colors=(0x000000FF, 0x00FFFFFF), refuse=frozenset()
):
    results = iter(find_results)
    finds: list[str] = []

    def find_window(_cls, title):
        finds.append(title)
        return next(results, 0)

    monkeypatch.setitem(
        sys.modules, "win32gui", types.SimpleNamespace(FindWindow=find_window)
    )
    dwm = _Dwm(refuse)
    monkeypatch.setattr(
        ctypes, "windll", types.SimpleNamespace(dwmapi=dwm), raising=False
    )
    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr("runspec_console.config.title_bar_colors", lambda: colors)
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


def test_unset_colour_leaves_the_system_title_bar(monkeypatch):
    """No `title_bar_color` = no restyle: no white-then-dark switch after load."""
    finds, dwm = _install(monkeypatch, [4242], colors=None)
    app._apply_dwm_title_bar("runspec console")
    assert finds == []
    assert dwm.calls == []


def test_old_windows_keeps_the_system_bar(monkeypatch):
    """Caption colour refused (Windows 10) ⇒ dark mode and border left alone."""
    _finds, dwm = _install(monkeypatch, [4242], refuse=frozenset({35}))
    app._apply_dwm_title_bar("Mycorp Console")
    assert [attr for _h, attr in dwm.calls] == [35]


def test_dark_mode_follows_the_text_colour(monkeypatch):
    # Red caption, white text ⇒ dark mode on (white caption buttons).
    _f, dwm = _install(monkeypatch, [1], colors=(0x001D0F7A, 0x00FFFFFF))
    app._apply_dwm_title_bar("t")
    assert dwm.values == {35: 0x001D0F7A, 36: 0x00FFFFFF, 34: 0x001D0F7A, 20: 1}
    # White caption, black text ⇒ dark mode off (dark caption buttons).
    _f, dwm = _install(monkeypatch, [1], colors=(0x00FFFFFF, 0x00000000))
    app._apply_dwm_title_bar("t")
    assert dwm.values[20] == 0


class _Handle:
    def __init__(self, value: int) -> None:
        self.value = value

    def ToInt32(self) -> int:  # noqa: N802
        return self.value


def test_before_show_tints_the_native_handle(monkeypatch):
    _f, dwm = _install(monkeypatch, [])
    monkeypatch.setattr(sys, "platform", "win32")
    window = types.SimpleNamespace(native=types.SimpleNamespace(Handle=_Handle(77)))
    app._apply_title_bar_before_show(window)
    assert {attr for _h, attr in dwm.calls} == {20, 34, 35, 36}
    assert all(h == 77 for h, _a in dwm.calls)


def test_before_show_is_a_noop_when_unset_or_broken(monkeypatch):
    _f, dwm = _install(monkeypatch, [], colors=None)
    monkeypatch.setattr(sys, "platform", "win32")
    window = types.SimpleNamespace(native=types.SimpleNamespace(Handle=_Handle(77)))
    app._apply_title_bar_before_show(window)
    assert dwm.calls == []
    _f, dwm = _install(monkeypatch, [])
    app._apply_title_bar_before_show(types.SimpleNamespace(native=None))
    assert dwm.calls == []

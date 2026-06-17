"""
Tests for the WebView2 default-context-menu enable (app._enable_native_context_menu).

The COM/WinForms control can't run off-Windows, so we drive the helper with fakes
that mimic the pywebview EdgeChromium object graph and assert it flips the right
setting — and is a safe no-op when the control or CoreWebView2 never appears.
"""

from __future__ import annotations

from types import SimpleNamespace

from runspec_console.app import _enable_native_context_menu


class _Settings:
    def __init__(self) -> None:
        self.AreDefaultContextMenusEnabled = False


def _window_with_core(core: object) -> object:
    # pywebview exposes the control via window.native; CoreWebView2 hangs off it.
    return SimpleNamespace(native=SimpleNamespace(CoreWebView2=core))


def test_enables_default_context_menu_when_core_ready():
    settings = _Settings()
    core = SimpleNamespace(Settings=settings)
    _enable_native_context_menu(_window_with_core(core))
    assert settings.AreDefaultContextMenusEnabled is True


def test_finds_core_behind_web_view_attribute():
    # Older pywebview nests the control under native.web_view.
    settings = _Settings()
    core = SimpleNamespace(Settings=settings)
    window = SimpleNamespace(
        native=SimpleNamespace(web_view=SimpleNamespace(CoreWebView2=core))
    )
    _enable_native_context_menu(window)
    assert settings.AreDefaultContextMenusEnabled is True


def test_noop_when_no_native_control():
    # No EdgeChromium control (e.g. another backend) → must not raise. timeout=0
    # checks once and returns, so the test doesn't wait on the poll.
    _enable_native_context_menu(SimpleNamespace(native=None), timeout=0.0)


def test_noop_when_setting_raises():
    # A control whose Settings access throws must be swallowed, not propagated.
    class _Boom:
        @property
        def Settings(self):  # noqa: N802 — mimics the .NET property name
            raise RuntimeError("RPC failure")

    _enable_native_context_menu(_window_with_core(_Boom()))  # no raise = pass

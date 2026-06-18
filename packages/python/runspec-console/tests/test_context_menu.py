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


def test_finds_core_deeply_nested():
    # pywebview winforms can nest it under native.browser.web_view — the BFS must
    # reach it regardless of the exact path.
    settings = _Settings()
    core = SimpleNamespace(Settings=settings)
    window = SimpleNamespace(
        native=SimpleNamespace(
            browser=SimpleNamespace(web_view=SimpleNamespace(CoreWebView2=core))
        )
    )
    _enable_native_context_menu(window)
    assert settings.AreDefaultContextMenusEnabled is True


def test_prints_diagnostic_when_not_found(capsys):
    # When the control can't be located, a one-line stderr diagnostic with the
    # native object's type + attrs is printed so the real path can be pinned.
    window = SimpleNamespace(native=SimpleNamespace(some_attr=1))
    _enable_native_context_menu(window, timeout=0.0)
    err = capsys.readouterr().err
    assert "CoreWebView2 not found" in err
    assert "some_attr" in err


def test_noop_when_no_native_control():
    # No EdgeChromium control (e.g. another backend) → must not raise. timeout=0
    # checks once and returns, so the test doesn't wait on the poll.
    _enable_native_context_menu(SimpleNamespace(native=None), timeout=0.0)


def test_finds_core_when_a_node_property_throws():
    # A real WebView2/pythonnet node can raise System.InvalidCastException from a
    # property getter (e.g. CoreWebView2 probed before init / off the UI thread).
    # getattr's default only swallows AttributeError, so such a throw used to abort
    # the whole BFS and leave the menu off. The walk must skip the bad node and
    # still reach a good CoreWebView2 elsewhere in the graph.
    settings = _Settings()
    core = SimpleNamespace(Settings=settings)

    class _Throwing:
        @property
        def CoreWebView2(self):  # noqa: N802 — mimics the .NET property name
            raise RuntimeError("InvalidCast: not ready")

    # native exposes a throwing sibling plus the real control under web_view.
    window = SimpleNamespace(
        native=SimpleNamespace(
            browser=_Throwing(),
            web_view=SimpleNamespace(CoreWebView2=core),
        )
    )
    _enable_native_context_menu(window)
    assert settings.AreDefaultContextMenusEnabled is True


def test_noop_when_setting_raises():
    # A control whose Settings access throws must be swallowed, not propagated.
    class _Boom:
        @property
        def Settings(self):  # noqa: N802 — mimics the .NET property name
            raise RuntimeError("RPC failure")

    _enable_native_context_menu(_window_with_core(_Boom()))  # no raise = pass

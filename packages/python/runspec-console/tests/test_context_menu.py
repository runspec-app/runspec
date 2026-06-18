"""
Tests for the WebView2 default-context-menu enable (app._enable_native_context_menu).

The right-click menu + spell-check suggestions are turned on via pywebview's own
settings (run in debug mode, which enables CoreWebView2's default context menu)
rather than by poking the live WebView2 control — so this just verifies the
settings handshake: debug is always requested, and the devtools *window* is gated
on the user's preference via OPEN_DEVTOOLS_IN_DEBUG.
"""

from __future__ import annotations

from types import SimpleNamespace

from runspec_console.app import _enable_native_context_menu


def _fake_webview() -> SimpleNamespace:
    # Mirror the pywebview module surface we touch: a mutable `settings` dict.
    return SimpleNamespace(settings={"OPEN_DEVTOOLS_IN_DEBUG": True})


def test_requests_debug_so_context_menu_is_enabled():
    # pywebview ties AreDefaultContextMenusEnabled to debug mode, so the helper
    # must always tell webview.start to run debug — that's what surfaces the
    # right-click menu + spell-check suggestions.
    webview = _fake_webview()
    assert _enable_native_context_menu(webview, open_devtools=False) is True


def test_devtools_window_stays_closed_by_default():
    # Menu on, inspector window off: OPEN_DEVTOOLS_IN_DEBUG must be pinned False
    # when the user didn't ask for devtools, so the inspector doesn't pop open.
    webview = _fake_webview()
    _enable_native_context_menu(webview, open_devtools=False)
    assert webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] is False


def test_devtools_window_opt_in_preserved():
    # When the user passes --dev/--devtools, the inspector window should open.
    webview = _fake_webview()
    debug = _enable_native_context_menu(webview, open_devtools=True)
    assert debug is True
    assert webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] is True

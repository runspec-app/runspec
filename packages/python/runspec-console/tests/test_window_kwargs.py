"""
test_window_kwargs.py — pin the main-window invariants that are easy to regress
and were the root cause of a shipped Linux bug.

`_window_kwargs()` was extracted from `app.main()` precisely so `private_mode`
can be asserted without launching a GUI. pywebview defaults private_mode=True,
which disables HTML5 local storage on the GTK/WebKit backend — window.localStorage
becomes null and the frontend crashes before it mounts. This test fails loudly if
that default ever creeps back in.
"""

from __future__ import annotations

from runspec_console import app


def test_window_kwargs_disables_private_mode():
    kw = app._window_kwargs()
    # The one that shipped a black screen on Linux: must be explicitly False.
    assert kw["private_mode"] is False


def test_window_kwargs_keeps_close_guard_and_selection():
    kw = app._window_kwargs()
    assert kw["confirm_close"] is True
    assert kw["text_select"] is True
    assert kw["min_size"] == (1024, 600)


def test_main_passes_window_kwargs_to_create_window(monkeypatch):
    """`main()` must actually spread `_window_kwargs()` into `create_window` —
    the helper is only a guard if the call site uses it. Drive `main()` far
    enough to reach `create_window`, capture its kwargs, then bail out."""
    import sys
    import types

    captured: dict[str, object] = {}

    class _Sentinel(Exception):
        pass

    def _fake_create_window(*_a, **kw):
        captured.update(kw)
        raise _Sentinel  # stop main() right after the call we care about

    fake_webview = types.SimpleNamespace(
        settings={},
        create_window=_fake_create_window,
        start=lambda *a, **k: None,
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    # Neutralise the heavy bring-up main() does before create_window.
    class _Args:
        dev = types.SimpleNamespace(value=True)  # dev mode → skips dist/static server
        port = types.SimpleNamespace(value=5173)
        devtools = types.SimpleNamespace(value=False)

    monkeypatch.setattr(app, "_suppress_noisy_loggers", lambda: None)

    import runspec

    monkeypatch.setattr(runspec, "parse", lambda _name: _Args())

    class _Bridge:
        def __init__(self, *a, **k):
            pass

        def set_debug(self, *a, **k):
            pass

        def set_window_title(self, *a, **k):
            pass

        def set_window(self, *a, **k):
            pass

        def mcp_start(self, *a, **k):
            pass

    import runspec_console.bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "Bridge", _Bridge)

    import runspec_console.notify as notify_mod

    monkeypatch.setattr(notify_mod, "set_app_id", lambda: None)

    import runspec_console.observability as obs_mod

    monkeypatch.setattr(obs_mod, "init_tracing", lambda *a, **k: None)
    monkeypatch.setattr(obs_mod, "shutdown", lambda *a, **k: None)

    import runspec_console.branding as branding_mod

    monkeypatch.setattr(
        branding_mod,
        "resolve_branding",
        lambda: types.SimpleNamespace(title="runspec"),
    )

    try:
        app.main()
    except _Sentinel:
        pass

    assert captured.get("private_mode") is False

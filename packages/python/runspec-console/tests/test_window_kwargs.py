"""
test_window_kwargs.py — pin the window/start invariants and, crucially, validate
them against the *real* pywebview signatures.

Background: `private_mode` is a parameter of `webview.start()`, NOT
`webview.create_window()`. A first cut passed it to `create_window`, which raises
`TypeError: create_window() got an unexpected keyword argument 'private_mode'` and
stops the app from starting on every platform. The earlier test mocked `webview`
with a `create_window(**kwargs)` stub, so it silently accepted the bad kwarg and
never caught the bug. `test_kwargs_match_pywebview_signatures` closes that hole by
checking each kwarg dict against the actual installed pywebview functions.
"""

from __future__ import annotations

import inspect

import pytest

from runspec_console import app


def test_window_kwargs_excludes_private_mode():
    # private_mode belongs on start(), not create_window() — keep it out of here.
    assert "private_mode" not in app._window_kwargs()


def test_window_kwargs_keeps_close_guard_and_selection():
    kw = app._window_kwargs()
    assert kw["confirm_close"] is True
    assert kw["text_select"] is True
    assert kw["min_size"] == (1024, 600)


def test_window_opens_wide():
    # Opens wide enough that Settings and the content pane aren't cramped.
    assert app._window_kwargs()["width"] >= 1680


def test_start_kwargs_disables_private_mode():
    kw = app._start_kwargs(debug=False, on_started=lambda: None, icon=None)
    # The one that shipped a black screen on Linux: must be explicitly False.
    assert kw["private_mode"] is False


def test_start_kwargs_includes_icon_only_when_present():
    with_icon = app._start_kwargs(debug=True, on_started=lambda: None, icon="/x/i.png")
    assert with_icon["icon"] == "/x/i.png"
    assert with_icon["debug"] is True

    without = app._start_kwargs(debug=False, on_started=lambda: None, icon=None)
    assert "icon" not in without


def _accepts(func, names: set[str]) -> tuple[bool, set[str]]:
    """(all names accepted?, offending names). A **kwargs param means anything
    goes, so we can't validate that side — treat as accepted."""
    sig = inspect.signature(func)
    if any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()):
        return True, set()
    valid = {
        n
        for n, p in sig.parameters.items()
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    }
    bad = names - valid
    return not bad, bad


def test_kwargs_match_pywebview_signatures():
    """Every kwarg we pass must be a real parameter of the pywebview function we
    pass it to. This is the check that would have caught private_mode-on-
    create_window. Skipped where pywebview isn't importable (e.g. the pure-logic
    test job); the console-app-smoke CI job installs it and runs this for real."""
    webview = pytest.importorskip("webview")

    ok_win, bad_win = _accepts(webview.create_window, set(app._window_kwargs()))
    assert ok_win, f"create_window() rejects: {bad_win}"

    start_keys = set(
        app._start_kwargs(debug=False, on_started=lambda: None, icon="/x/i.png")
    )
    ok_start, bad_start = _accepts(webview.start, start_keys)
    assert ok_start, f"start() rejects: {bad_start}"


def test_main_wires_create_window_and_start(monkeypatch):
    """Drive `main()` end-to-end with a fake webview and assert the split lands
    correctly: create_window gets the window kwargs *without* private_mode, and
    start gets private_mode=False."""
    import sys
    import types

    win_kwargs: dict[str, object] = {}
    start_captured: dict[str, object] = {}

    class _Stop(Exception):
        pass

    def _fake_create_window(*_a, **kw):
        win_kwargs.update(kw)
        return object()

    def _fake_start(**kw):
        start_captured.update(kw)
        raise _Stop  # stop main() right after the call we care about

    fake_webview = types.SimpleNamespace(
        settings={},
        create_window=_fake_create_window,
        start=_fake_start,
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    class _Args:
        dev = types.SimpleNamespace(value=True)  # dev mode → skips dist/static server
        port = types.SimpleNamespace(value=5173)
        devtools = types.SimpleNamespace(value=False)
        config_dir = types.SimpleNamespace(value=None)  # no --config-dir override

    monkeypatch.setattr(app, "_suppress_noisy_loggers", lambda: None)

    import runspec

    monkeypatch.setattr(runspec, "parse", lambda _name: _Args())

    class _Bridge:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, _name):  # set_debug / set_window / mcp_start / …
            return lambda *a, **k: None

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
    except _Stop:
        pass

    assert "private_mode" not in win_kwargs  # not on create_window
    assert start_captured.get("private_mode") is False  # yes on start

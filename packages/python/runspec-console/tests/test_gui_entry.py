"""
The windowed (gui-scripts) entry point — main_gui.

pip builds `runspec-console-app` against pythonw.exe (no console window). Because
there's no terminal, a startup crash must not vanish: main_gui writes the
traceback to a crash log and shows a native dialog, then exits non-zero. The
plain `runspec-console` console script is unchanged (tracebacks to the terminal).
"""

from __future__ import annotations

import sys

import pytest

import runspec_console.app as app


def test_ensure_std_streams_replaces_none(monkeypatch):
    # Simulate pythonw: no console → sys.stdin/stdout/stderr are None. Probing
    # .isatty() (runspec core's parse(), colorama, …) must not crash.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdin", None)

    app._ensure_std_streams()

    for stream in (sys.stdout, sys.stderr, sys.stdin):
        assert stream is not None
        # The exact call that used to blow up on None must now just work. The
        # value is platform-dependent (POSIX /dev/null → False; on Windows NUL is
        # a FILE_TYPE_CHAR device, so isatty() is True) — the contract is that it
        # returns a bool without raising, not a specific value.
        assert isinstance(stream.isatty(), bool)


def test_ensure_std_streams_leaves_real_streams(monkeypatch):
    # A real terminal / the console script: streams exist → no-op, not replaced.
    sentinel_out, sentinel_err = sys.stdout, sys.stderr
    app._ensure_std_streams()
    assert sys.stdout is sentinel_out
    assert sys.stderr is sentinel_err


def test_startup_profiler_off_by_default(monkeypatch, capsys):
    monkeypatch.delenv("RUNSPEC_STARTUP_PROFILE", raising=False)
    monkeypatch.setattr(sys, "argv", ["runspec-console"])
    mark = app._startup_profiler()
    mark("phase one")  # must be a silent no-op
    assert capsys.readouterr().err == ""


def test_startup_profiler_on_emits_phase_lines(monkeypatch, capsys):
    monkeypatch.setenv("RUNSPEC_STARTUP_PROFILE", "1")
    mark = app._startup_profiler()  # emits "main() entered"
    mark("phase one")
    err = capsys.readouterr().err
    assert "startup[" in err
    assert "main() entered" in err
    assert "phase one" in err


def test_startup_profile_requested_env(monkeypatch):
    monkeypatch.setenv("RUNSPEC_STARTUP_PROFILE", "1")
    monkeypatch.setattr(sys, "argv", ["runspec-console"])
    assert app._startup_profile_requested() is True


def test_startup_profile_requested_cli_flag(monkeypatch):
    # The flag is scanned off sys.argv because the profiler runs before parse().
    monkeypatch.delenv("RUNSPEC_STARTUP_PROFILE", raising=False)
    monkeypatch.setattr(sys, "argv", ["runspec-console", "--profile-startup"])
    assert app._startup_profile_requested() is True


def test_startup_profile_requested_off(monkeypatch):
    monkeypatch.delenv("RUNSPEC_STARTUP_PROFILE", raising=False)
    monkeypatch.setattr(sys, "argv", ["runspec-console", "--dev"])
    assert app._startup_profile_requested() is False


def test_main_gui_runs_main_on_success(monkeypatch):
    called: list[bool] = []
    monkeypatch.setattr(app, "main", lambda: called.append(True))
    # A clean run returns normally (no dialog, no exit).
    monkeypatch.setattr(
        app, "_show_startup_error", lambda *a, **k: pytest.fail("no error expected")
    )
    app.main_gui()
    assert called == [True]


def test_main_gui_reports_crash_to_logfile_and_exits(monkeypatch, tmp_path):
    def boom() -> None:
        raise RuntimeError("kaboom-42")

    monkeypatch.setattr(app, "main", boom)
    log = tmp_path / "startup-crash.log"
    monkeypatch.setattr(app, "_crash_log_path", lambda: str(log))
    shown: dict[str, object] = {}
    monkeypatch.setattr(
        app,
        "_show_startup_error",
        lambda summary, path: shown.update(summary=summary, path=path),
    )

    with pytest.raises(SystemExit) as exc:
        app.main_gui()

    assert exc.value.code == 1
    # The full traceback is durably written where the user can find it.
    assert log.exists()
    body = log.read_text(encoding="utf-8")
    assert "kaboom-42" in body and "startup crash" in body
    # The dialog got the concise error line + the log path.
    assert "kaboom-42" in str(shown["summary"])
    assert shown["path"] == str(log)


def test_main_gui_lets_systemexit_through(monkeypatch):
    # argparse --help / an explicit sys.exit must not be swallowed by the guard.
    def bail() -> None:
        raise SystemExit(0)

    monkeypatch.setattr(app, "main", bail)
    with pytest.raises(SystemExit) as exc:
        app.main_gui()
    assert exc.value.code == 0


def test_shutdown_telemetry_calls_both(monkeypatch):
    import runspec_console.observability as obs

    calls: list[str] = []
    monkeypatch.setattr(obs, "shutdown", lambda: calls.append("spans"))
    monkeypatch.setattr(obs, "shutdown_metrics", lambda: calls.append("metrics"))
    app._shutdown_telemetry(timeout=2.0)
    assert "spans" in calls and "metrics" in calls


def test_shutdown_telemetry_bounded_when_flush_hangs(monkeypatch):
    # A slow/unreachable OTLP endpoint makes force_flush block (~18s seen closing
    # against a stopped Phoenix); the app close must not wait it out. The bounded
    # helper returns well before the flush would finish.
    import time as _time

    import runspec_console.observability as obs

    monkeypatch.setattr(obs, "shutdown", lambda: _time.sleep(5))
    monkeypatch.setattr(obs, "shutdown_metrics", lambda: None)
    start = _time.perf_counter()
    app._shutdown_telemetry(timeout=0.2)
    assert _time.perf_counter() - start < 2.0

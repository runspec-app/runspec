"""Tests for the run-summary feature in logging_setup.py."""

from __future__ import annotations

import json
import logging
import sys

import pytest

import runspec.logging_setup as ls
from runspec.logging_setup import (
    _ConsoleFormatter,
    _emit_run_summary,
    _get_invoker,
    _JsonFormatter,
    _RunSummaryCounter,
    _SensitiveFilter,
    configure_logging,
)


@pytest.fixture(autouse=True)
def _venv_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Reset logging state and unwind any atexit hooks left between tests."""
    # Prevent real atexit registration from running on interpreter shutdown
    # mid-test — patch atexit.register to a no-op for the duration of the test.
    monkeypatch.setattr(ls.atexit, "register", lambda fn, *args, **kwargs: fn)
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(getattr(h, "formatter", None), (_JsonFormatter, _ConsoleFormatter)):
            root.removeHandler(h)
            h.close()
        elif isinstance(h, _RunSummaryCounter):
            root.removeHandler(h)
    for f in list(root.filters):
        if isinstance(f, _SensitiveFilter):
            root.removeFilter(f)
    ls._configured = False
    ls._summary_state = None
    ls._debug = False


def _cfg(summary=True):
    return {"rotate": "midnight", "keep": 7, "summary": summary}


# ── Counter ──────────────────────────────────────────────────────────────────


class TestCounter:
    def test_counter_increments_per_level(self):
        configure_logging(_cfg(), runnable_name="x")
        log = logging.getLogger("test.counter")
        log.info("one")
        log.info("two")
        log.warning("careful")
        log.error("broke")
        assert ls._summary_state is not None
        counts = ls._summary_state["counter"].counts
        assert counts["INFO"] == 2
        assert counts["WARNING"] == 1
        assert counts["ERROR"] == 1
        assert counts["CRITICAL"] == 0

    def test_counter_ignores_summary_logger(self):
        """The summary record itself must not inflate counts."""
        configure_logging(_cfg(), runnable_name="x")
        logging.getLogger(ls._RUN_SUMMARY_LOGGER).info("not counted")
        counts = ls._summary_state["counter"].counts
        assert counts["INFO"] == 0


# ── Atexit emission ──────────────────────────────────────────────────────────


class TestEmit:
    def test_summary_writes_record_to_file(self, tmp_path, capsys):
        configure_logging(_cfg(), runnable_name="myscript", agent=False, autonomy="confirm")
        logging.getLogger("test.emit").info("did work")
        logging.getLogger("test.emit").warning("a warning")
        _emit_run_summary()

        log_path = tmp_path / "logs" / "myscript.log"
        lines = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
        summaries = [obj for obj in lines if obj["logger"] == "runspec.runsummary"]
        assert len(summaries) == 1
        s = summaries[0]
        assert s["message"] == "run completed"
        assert s["extra"]["event"] == "run_summary"
        assert s["extra"]["runnable"] == "myscript"
        assert s["extra"]["events"]["INFO"] == 1
        assert s["extra"]["events"]["WARNING"] == 1
        assert s["extra"]["exit_code"] == 0
        assert s["extra"]["exception"] is None

    def test_summary_writes_line_to_stderr(self, capsys):
        configure_logging(_cfg(), runnable_name="myscript")
        logging.getLogger("test.stderr").info("ran")
        _emit_run_summary()
        err = capsys.readouterr().err
        assert "runspec: myscript completed" in err
        assert "events" in err

    def test_summary_not_in_console_handlers(self, capsys):
        """The JSON form is file-only — neither stdout nor stderr should contain it."""
        configure_logging(_cfg(), runnable_name="myscript")
        logging.getLogger("test.console").info("hi")
        _emit_run_summary()
        cap = capsys.readouterr()
        # The console handlers must not have emitted the runspec.runsummary JSON.
        assert '"logger": "runspec.runsummary"' not in cap.out
        assert '"logger": "runspec.runsummary"' not in cap.err

    def test_summary_idempotent(self, tmp_path):
        configure_logging(_cfg(), runnable_name="myscript")
        _emit_run_summary()
        _emit_run_summary()
        log_path = tmp_path / "logs" / "myscript.log"
        lines = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
        summaries = [obj for obj in lines if obj["logger"] == "runspec.runsummary"]
        assert len(summaries) == 1


# ── Exception capture ────────────────────────────────────────────────────────


class TestExceptionCapture:
    def test_excepthook_records_exception(self, capsys):
        configure_logging(_cfg(), runnable_name="x")
        # Simulate an uncaught exception
        try:
            raise ValueError("boom")
        except ValueError:
            exc_type, exc_value, tb = sys.exc_info()
            sys.excepthook(exc_type, exc_value, tb)
        assert ls._summary_state["exception"] is not None
        assert ls._summary_state["exception"]["type"] == "ValueError"
        assert ls._summary_state["exception"]["message"] == "boom"

    def test_failure_summary_includes_exception_class(self, capsys):
        configure_logging(_cfg(), runnable_name="failing")
        try:
            raise RuntimeError("bad")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())
        _emit_run_summary()
        err = capsys.readouterr().err
        assert "runspec: failing failed" in err
        assert "exit 1" in err
        assert "RuntimeError" in err

    def _read_exc_record(self, tmp_path, runnable):
        """Return the parsed runspec.exception record from the audit log, or None."""
        log_path = tmp_path / "logs" / f"{runnable}.log"
        for line in log_path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("logger") == "runspec.exception":
                return rec
        return None

    def test_structured_record_written_even_without_summary(self, tmp_path, capsys):
        # The decoupling fix: with summary OFF the exception must still reach the file.
        configure_logging(_cfg(summary=False), runnable_name="nosum")
        assert ls._summary_state is None  # summary genuinely disabled
        try:
            raise ValueError("invalid quality 200")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        rec = self._read_exc_record(tmp_path, "nosum")
        assert rec is not None
        assert rec["level"] == "CRITICAL"
        es = rec["exc_structured"]
        assert es["type"] == "ValueError"
        assert es["message"] == "invalid quality 200"
        assert es["module"] == "test_run_summary"
        assert es["frames"][-1]["func"] == "test_structured_record_written_even_without_summary"
        assert "exc" in rec  # full string traceback also present

    def test_no_debug_prints_one_liner_not_traceback(self, tmp_path, capsys):
        configure_logging(_cfg(), runnable_name="quiet", debug=False)
        try:
            raise ValueError("boom")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        err = capsys.readouterr().err
        assert "ERROR: ValueError: boom" in err
        assert "run with --debug" in err
        assert "Traceback (most recent call last)" not in err
        # File still carries the full structured form.
        assert self._read_exc_record(tmp_path, "quiet") is not None

    def test_debug_prints_compact_trace(self, tmp_path, capsys):
        configure_logging(_cfg(), runnable_name="loud", debug=True)
        try:
            raise ValueError("boom")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        err = capsys.readouterr().err
        assert "ValueError: boom" in err
        assert "test_run_summary.py:" in err  # compact frame line (basename)
        assert "run with --debug" not in err  # the hint only shows in quiet mode

    def test_exception_record_not_double_printed_to_console(self, tmp_path, capsys):
        # The structured CRITICAL record must be dropped by the stdout/stderr
        # handlers (routed file-only); only our explicit one-liner appears.
        configure_logging(_cfg(), runnable_name="x", debug=False)
        try:
            raise ValueError("boom")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        captured = capsys.readouterr()
        assert "uncaught exception" not in captured.err  # the logger message text
        assert "uncaught exception" not in captured.out


class TestExcStructuredHelpers:
    def test_build_exc_structured_shape(self):
        try:
            raise KeyError("missing")
        except KeyError:
            es = ls._build_exc_structured(*sys.exc_info())
        assert es["type"] == "KeyError"
        assert es["module"] == "test_run_summary"
        assert es["frames"]
        assert set(es["frames"][0]) == {"file", "line", "func", "code"}

    def test_compact_traceback_filters_internal_frames(self):
        # A frame inside the runspec package dir is dropped from the display,
        # user frames are kept; full list is preserved by the caller.
        frames = [
            {"file": ls._RUNSPEC_DIR + "/parser.py", "line": 10, "func": "parse", "code": "do()"},
            {"file": "/app/deploy.py", "line": 42, "func": "main", "code": "boom()"},
        ]
        out = ls._format_compact_traceback(ValueError, ValueError("x"), frames)
        assert "deploy.py:42" in out
        assert "parser.py:10" not in out

    def test_compact_traceback_falls_back_when_all_internal(self):
        frames = [{"file": ls._RUNSPEC_DIR + "/parser.py", "line": 10, "func": "parse", "code": "do()"}]
        out = ls._format_compact_traceback(ValueError, ValueError("x"), frames)
        assert "parser.py:10" in out  # not left empty


# ── Disable switches ─────────────────────────────────────────────────────────


class TestDisable:
    def test_no_summary_param_disables_summary(self, capsys, tmp_path):
        configure_logging(_cfg(summary=True), runnable_name="x", no_summary=True)
        # _summary_state must NOT be populated when summary is disabled
        assert ls._summary_state is None

    def test_summary_false_in_config_disables(self):
        configure_logging(_cfg(summary=False), runnable_name="x")
        assert ls._summary_state is None

    def test_env_var_disables_summary(self, monkeypatch):
        monkeypatch.setenv("RUNSPEC_X_ARG_NO_SUMMARY", "1")
        configure_logging(_cfg(summary=True), runnable_name="x")
        assert ls._summary_state is None


# ── Stderr line shape ────────────────────────────────────────────────────────


class TestStderrLine:
    def test_success_line_singular_grammar(self, capsys):
        configure_logging(_cfg(), runnable_name="r")
        logging.getLogger("t").warning("one")
        _emit_run_summary()
        err = capsys.readouterr().err
        assert "1 warning," in err  # singular
        assert "0 errors)" in err  # plural for zero

    def test_success_line_plural_grammar(self, capsys):
        configure_logging(_cfg(), runnable_name="r")
        logging.getLogger("t").warning("a")
        logging.getLogger("t").warning("b")
        _emit_run_summary()
        err = capsys.readouterr().err
        assert "2 warnings," in err


# ── Invoker capture ──────────────────────────────────────────────────────────


class TestInvoker:
    def test_no_sudo_returns_user(self, monkeypatch):
        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setenv("USER", "alice")
        user, target = _get_invoker()
        assert user == "alice"
        assert target is None

    def test_sudo_returns_real_user_and_target(self, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "alice")
        monkeypatch.setenv("USER", "root")
        user, target = _get_invoker()
        assert user == "alice"
        assert target == "root"

    def test_user_appended_to_stderr_line(self, capsys, monkeypatch):
        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setenv("USER", "alice")
        configure_logging(_cfg(), runnable_name="r")
        _emit_run_summary()
        err = capsys.readouterr().err
        assert "| user: alice" in err

    def test_sudo_user_shown_with_target(self, capsys, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "alice")
        monkeypatch.setenv("USER", "root")
        configure_logging(_cfg(), runnable_name="r")
        _emit_run_summary()
        err = capsys.readouterr().err
        assert "| user: alice → root (sudo)" in err

    def test_user_in_audit_record(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setenv("USER", "alice")
        configure_logging(_cfg(), runnable_name="myscript")
        _emit_run_summary()
        log_path = tmp_path / "logs" / "myscript.log"
        lines = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
        summary = next(s for s in lines if s["logger"] == "runspec.runsummary")
        assert summary["extra"]["user"] == "alice"
        assert summary["extra"]["user_target"] is None

    def test_sudo_user_target_in_audit_record(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "alice")
        monkeypatch.setenv("USER", "root")
        configure_logging(_cfg(), runnable_name="myscript")
        _emit_run_summary()
        log_path = tmp_path / "logs" / "myscript.log"
        lines = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
        summary = next(s for s in lines if s["logger"] == "runspec.runsummary")
        assert summary["extra"]["user"] == "alice"
        assert summary["extra"]["user_target"] == "root"

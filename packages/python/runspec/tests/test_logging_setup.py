"""Tests for logging_setup.py — stdlib logging configuration from [config.logging]."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path

import pytest

import runspec.logging_setup as ls
from runspec.logging_setup import (
    _ConsoleFormatter,
    _JsonFormatter,
    _make_file_handler,
    _resolve_log_dir,
    _SensitiveFilter,
    configure_logging,
)


def _our_console_handlers():
    """Return console handlers added by our code (identified by _ConsoleFormatter)."""
    return [h for h in logging.getLogger().handlers if isinstance(getattr(h, "formatter", None), _ConsoleFormatter)]


def _our_stdout_handler():
    """The handler routing INFO and below to stdout."""
    for h in _our_console_handlers():
        if getattr(h, "stream", None) is sys.stdout:
            return h
    return None


def _our_stderr_handler():
    """The handler routing WARNING and above to stderr."""
    for h in _our_console_handlers():
        if getattr(h, "stream", None) is sys.stderr:
            return h
    return None


def _our_file_handlers():
    """Return file handlers added by our code (identified by _JsonFormatter)."""
    return [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler) and isinstance(getattr(h, "formatter", None), _JsonFormatter)]


# ── Shared fixture ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_logging():
    """Reset logging state between tests to prevent handler accumulation."""
    yield
    root = logging.getLogger()
    # Remove only OUR handlers, identified by formatter — robust against pytest's own handlers
    for h in list(root.handlers):
        if isinstance(getattr(h, "formatter", None), (_JsonFormatter, _ConsoleFormatter)):
            root.removeHandler(h)
            h.close()
    for f in list(root.filters):
        if isinstance(f, _SensitiveFilter):
            root.removeFilter(f)
    # Direct assignment — do NOT use monkeypatch here; it would restore
    # the True value from within teardown on cleanup.
    ls._configured = False


# ── TestNoop ──────────────────────────────────────────────────────────────────


class TestNoop:
    def test_none_config_is_no_op(self, tmp_path):
        before = len(logging.getLogger().handlers)
        configure_logging(None, runnable_name="x", config_path=tmp_path / "runspec.toml")
        assert len(logging.getLogger().handlers) == before

    def test_idempotent_second_call_ignored(self, tmp_path):
        cfg = {"rotate": "midnight", "keep": 7}
        toml = tmp_path / "runspec.toml"
        configure_logging(cfg, runnable_name="x", config_path=toml)
        first_our_count = len(_our_file_handlers()) + len(_our_console_handlers())
        configure_logging(cfg, runnable_name="x", config_path=toml)
        assert len(_our_file_handlers()) + len(_our_console_handlers()) == first_our_count


# ── TestConsoleRouting ────────────────────────────────────────────────────────


class TestConsoleRouting:
    """INFO+below → stdout (plain print), WARNING+ → stderr (prefixed)."""

    def _cfg(self):
        return {"rotate": "midnight", "keep": 7}

    def test_both_console_handlers_added(self, tmp_path):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml")
        assert _our_stdout_handler() is not None
        assert _our_stderr_handler() is not None

    def test_info_routes_to_stdout(self, tmp_path, capsys):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml")
        logging.getLogger("test.route").info("hello from info")
        cap = capsys.readouterr()
        assert "hello from info" in cap.out
        assert "hello from info" not in cap.err

    def test_warning_routes_to_stderr(self, tmp_path, capsys):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml")
        logging.getLogger("test.route").warning("be careful")
        cap = capsys.readouterr()
        assert "be careful" in cap.err
        assert "be careful" not in cap.out

    def test_error_routes_to_stderr(self, tmp_path, capsys):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml")
        logging.getLogger("test.route").error("something broke")
        cap = capsys.readouterr()
        assert "something broke" in cap.err
        assert "something broke" not in cap.out

    def test_info_format_is_plain_message(self, tmp_path, capsys):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml")
        logging.getLogger("test.fmt").info("hello world")
        line = capsys.readouterr().out.strip()
        # No timestamp, no level prefix, no logger name — reads like print()
        assert line == "hello world"
        assert not re.search(r"\d{2}:\d{2}:\d{2}", line)

    def test_warning_format_has_level_prefix(self, tmp_path, capsys):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml")
        logging.getLogger("test.fmt").warning("heads up")
        line = capsys.readouterr().err.strip()
        assert line == "WARNING: heads up"

    def test_error_format_has_level_prefix(self, tmp_path, capsys):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml")
        logging.getLogger("test.fmt").error("broke")
        line = capsys.readouterr().err.strip()
        assert line == "ERROR: broke"

    def test_critical_format_has_level_prefix(self, tmp_path, capsys):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml")
        logging.getLogger("test.fmt").critical("dead")
        line = capsys.readouterr().err.strip()
        assert line == "CRITICAL: dead"

    def test_default_mode_hides_traceback(self, tmp_path, capsys):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml")
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("test.human.info").error("something went wrong", exc_info=True)
        err = capsys.readouterr().err
        assert "Traceback" not in err
        assert "something went wrong" in err

    def test_debug_flag_shows_traceback(self, tmp_path, capsys):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml", debug=True)
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("test.human.debug").error("oops", exc_info=True)
        err = capsys.readouterr().err
        assert "Traceback" in err

    def test_debug_flag_routes_debug_records_to_stdout_with_location(self, tmp_path, capsys):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml", debug=True)
        logging.getLogger("test.location").debug("at location")
        out = capsys.readouterr().out
        assert "DEBUG" in out
        assert ".py:" in out

    def test_debug_records_silent_without_debug_flag(self, tmp_path, capsys):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml")
        logging.getLogger("test.threshold").debug("not shown")
        cap = capsys.readouterr()
        assert "not shown" not in cap.out
        assert "not shown" not in cap.err

    def test_file_handler_present(self, tmp_path):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml")
        assert len(_our_file_handlers()) == 1


# ── TestFileLogging ───────────────────────────────────────────────────────────


class TestFileLogging:
    def _cfg(self, rotate="midnight", keep=7):
        return {"rotate": rotate, "keep": keep}

    def test_file_created_in_logs_subdir(self, tmp_path):
        toml = tmp_path / "pkg" / "runspec.toml"
        toml.parent.mkdir()
        configure_logging(self._cfg(), runnable_name="myscript", config_path=toml)
        assert (tmp_path / "pkg" / "logs" / "myscript.log").exists()

    def test_file_content_is_json_lines(self, tmp_path):
        toml = tmp_path / "pkg" / "runspec.toml"
        toml.parent.mkdir()
        configure_logging(self._cfg(), runnable_name="myscript", config_path=toml)
        logging.getLogger("test.file").info("hello from file")
        for h in _our_file_handlers():
            h.flush()
        log_file = tmp_path / "pkg" / "logs" / "myscript.log"
        obj = json.loads(log_file.read_text().strip().splitlines()[0])
        assert obj["level"] == "INFO"
        assert obj["message"] == "hello from file"
        assert "ts" in obj
        assert "logger" in obj

    def test_file_captures_debug_regardless_of_console_level(self, tmp_path):
        toml = tmp_path / "pkg" / "runspec.toml"
        toml.parent.mkdir()
        configure_logging(self._cfg(), runnable_name="myscript", config_path=toml)
        logging.getLogger("test.file.debug").debug("debug message")
        for h in _our_file_handlers():
            h.flush()
        log_file = tmp_path / "pkg" / "logs" / "myscript.log"
        lines = [json.loads(line) for line in log_file.read_text().strip().splitlines()]
        assert any(obj["level"] == "DEBUG" for obj in lines)

    def test_exc_field_in_json_on_exception(self, tmp_path):
        toml = tmp_path / "pkg" / "runspec.toml"
        toml.parent.mkdir()
        configure_logging(self._cfg(), runnable_name="myscript", config_path=toml)
        try:
            raise RuntimeError("test error")
        except RuntimeError:
            logging.getLogger("test.exc").error("oops", exc_info=True)
        for h in _our_file_handlers():
            h.flush()
        log_file = tmp_path / "pkg" / "logs" / "myscript.log"
        lines = [json.loads(line) for line in log_file.read_text().strip().splitlines()]
        assert any("exc" in obj for obj in lines)


# ── TestFallbackLogDir ────────────────────────────────────────────────────────


class TestFallbackLogDir:
    def test_falls_back_to_home_logs_on_permission_error(self, tmp_path, monkeypatch):
        """Falls back to ~/logs when the candidate directory probe fails."""
        home_dir = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home_dir))

        toml = tmp_path / "pkg" / "runspec.toml"
        toml.parent.mkdir(parents=True, exist_ok=True)

        # Make the write-probe fail on the candidate dir to simulate no write permission
        orig_touch = Path.touch

        def failing_touch(self, mode=0o666, exist_ok=True):
            if self.name == ".wtest":
                raise PermissionError("simulated no-write")
            return orig_touch(self, mode=mode, exist_ok=exist_ok)

        monkeypatch.setattr(Path, "touch", failing_touch)

        result = _resolve_log_dir(toml)
        assert result == home_dir / "logs"


# ── TestRotation ──────────────────────────────────────────────────────────────


class TestRotation:
    def test_size_rotation_mb(self, tmp_path):
        h = _make_file_handler(tmp_path / "app.log", "10 MB", 5)
        assert isinstance(h, logging.handlers.RotatingFileHandler)
        assert h.maxBytes == 10 * 1024 * 1024
        h.close()

    def test_size_rotation_kb(self, tmp_path):
        h = _make_file_handler(tmp_path / "app.log", "100 KB", 3)
        assert isinstance(h, logging.handlers.RotatingFileHandler)
        assert h.maxBytes == 100 * 1024
        h.close()

    def test_size_rotation_gb(self, tmp_path):
        h = _make_file_handler(tmp_path / "app.log", "1 GB", 2)
        assert isinstance(h, logging.handlers.RotatingFileHandler)
        assert h.maxBytes == 1024**3
        h.close()

    def test_timed_midnight(self, tmp_path):
        h = _make_file_handler(tmp_path / "app.log", "midnight", 7)
        assert isinstance(h, logging.handlers.TimedRotatingFileHandler)
        assert h.when == "MIDNIGHT"
        h.close()

    def test_timed_daily(self, tmp_path):
        h = _make_file_handler(tmp_path / "app.log", "daily", 7)
        assert isinstance(h, logging.handlers.TimedRotatingFileHandler)
        assert h.when == "D"
        h.close()

    def test_timed_weekly(self, tmp_path):
        h = _make_file_handler(tmp_path / "app.log", "weekly", 7)
        assert isinstance(h, logging.handlers.TimedRotatingFileHandler)
        assert h.when == "W0"
        h.close()

    def test_invalid_rotate_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="not recognised"):
            _make_file_handler(tmp_path / "app.log", "monthly", 7)


# ── TestLogLevelOverride ──────────────────────────────────────────────────────


class TestDebugFlag:
    def _cfg(self):
        return {"rotate": "midnight", "keep": 7}

    def test_default_stdout_floor_is_info(self, tmp_path):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml")
        stdout = _our_stdout_handler()
        assert stdout is not None
        assert stdout.level == logging.INFO

    def test_debug_flag_lowers_stdout_floor_to_debug(self, tmp_path):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml", debug=True)
        stdout = _our_stdout_handler()
        assert stdout is not None
        assert stdout.level == logging.DEBUG

    def test_stderr_floor_is_always_warning(self, tmp_path):
        # Independent of the debug flag — warnings must never be silenced.
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml", debug=True)
        stderr = _our_stderr_handler()
        assert stderr is not None
        assert stderr.level == logging.WARNING

    def test_debug_flag_does_not_affect_file_level(self, tmp_path):
        configure_logging(self._cfg(), runnable_name="x", config_path=tmp_path / "runspec.toml", debug=True)
        handlers = _our_file_handlers()
        assert handlers[0].level == logging.DEBUG


# ── TestSensitiveFilter ───────────────────────────────────────────────────────


class TestSensitiveFilter:
    def _make_record(self, msg: str) -> logging.LogRecord:
        return logging.LogRecord("test", logging.INFO, "", 0, msg, None, None)

    def test_password_redacted(self):
        f = _SensitiveFilter()
        r = self._make_record("password=supersecret")
        f.filter(r)
        assert "supersecret" not in r.msg
        assert "REDACTED" in r.msg

    def test_token_redacted(self):
        f = _SensitiveFilter()
        r = self._make_record("token=abc123xyz")
        f.filter(r)
        assert "abc123xyz" not in r.msg

    def test_bearer_auth_redacted(self):
        f = _SensitiveFilter()
        r = self._make_record("Authorization: Bearer eyJhbGci.payload.sig")
        f.filter(r)
        assert "eyJhbGci" not in r.msg
        assert "REDACTED" in r.msg

    def test_url_credentials_redacted(self):
        f = _SensitiveFilter()
        r = self._make_record("Connecting to https://user:mypassword@example.com/api")
        f.filter(r)
        assert "mypassword" not in r.msg

    def test_json_password_field_redacted(self):
        f = _SensitiveFilter()
        r = self._make_record('{"username": "alice", "password": "s3cr3t"}')
        f.filter(r)
        assert "s3cr3t" not in r.msg

    def test_form_encoded_token_redacted(self):
        f = _SensitiveFilter()
        r = self._make_record("grant_type=password&token=abc&scope=read")
        f.filter(r)
        assert "abc" not in r.msg or "REDACTED" in r.msg

    def test_filter_always_returns_true(self):
        f = _SensitiveFilter()
        # Record with mismatched args — getMessage() will raise; filter must still return True
        r = logging.LogRecord("test", logging.INFO, "", 0, "msg %s %s", ("only_one",), None)
        assert f.filter(r) is True

    def test_filter_silent_on_error(self):
        f = _SensitiveFilter()
        r = logging.LogRecord("test", logging.INFO, "", 0, None, None, None)
        assert f.filter(r) is True


# ── TestGetLogger ─────────────────────────────────────────────────────────────


class TestGetLogger:
    def test_module_level_getlogger_works_after_parse(self, tmp_path, capsys):
        """Logger created before configure_logging() emits correctly after."""
        early_logger = logging.getLogger("early.module.unique")
        cfg = {"rotate": "midnight", "keep": 7}
        configure_logging(cfg, runnable_name="x", config_path=tmp_path / "runspec.toml")
        early_logger.info("message from early logger")
        out = capsys.readouterr().out
        assert "message from early logger" in out


# ── TestExtraFields ──────────────────────────────────────────────────────────


class TestExtraFields:
    def test_extra_fields_appear_in_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ls, "_configured", False)
        logging.getLogger().handlers.clear()
        configure_logging(
            {"rotate": "midnight", "keep": 7},
            runnable_name="myscript",
            config_path=tmp_path / "runspec.toml",
        )
        logging.getLogger("test").info("connected", extra={"user_id": "42", "region": "eu-west"})
        log_path = tmp_path / "logs" / "myscript.log"
        record = json.loads(log_path.read_text().strip())
        assert record["extra"] == {"user_id": "42", "region": "eu-west"}
        assert record["message"] == "connected"

    def test_no_extra_key_when_no_extra(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ls, "_configured", False)
        logging.getLogger().handlers.clear()
        configure_logging(
            {"rotate": "midnight", "keep": 7},
            runnable_name="myscript",
            config_path=tmp_path / "runspec.toml",
        )
        logging.getLogger("test").info("plain message")
        log_path = tmp_path / "logs" / "myscript.log"
        record = json.loads(log_path.read_text().strip())
        assert "extra" not in record

    def test_extra_fields_redacted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ls, "_configured", False)
        logging.getLogger().handlers.clear()
        configure_logging(
            {"rotate": "midnight", "keep": 7},
            runnable_name="myscript",
            config_path=tmp_path / "runspec.toml",
        )
        logging.getLogger("test").info("auth", extra={"token": "secret123", "user": "alice"})
        log_path = tmp_path / "logs" / "myscript.log"
        content = log_path.read_text()
        assert "secret123" not in content
        assert "[REDACTED]" in content
        assert "alice" in content  # non-sensitive field untouched

    def test_extra_fields_appear_in_console(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(ls, "_configured", False)
        logging.getLogger().handlers.clear()
        configure_logging(
            {"rotate": "midnight", "keep": 7},
            runnable_name="myscript",
            config_path=tmp_path / "runspec.toml",
        )
        logging.getLogger("test").info("connected", extra={"user_id": "42"})
        out = capsys.readouterr().out
        assert "user_id=42" in out

    def test_extra_integer_field(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ls, "_configured", False)
        logging.getLogger().handlers.clear()
        configure_logging(
            {"rotate": "midnight", "keep": 7},
            runnable_name="myscript",
            config_path=tmp_path / "runspec.toml",
        )
        logging.getLogger("test").info("counts", extra={"items": 99})
        log_path = tmp_path / "logs" / "myscript.log"
        record = json.loads(log_path.read_text().strip())
        assert record["extra"]["items"] == 99


# ── TestRunSpecPrefix ─────────────────────────────────────────────────────────


class TestRunSpecPrefix:
    def test_runspec_prefix_returns_parent_of_source(self, tmp_path):
        from runspec.models import RunSpec

        toml = tmp_path / "mypkg" / "runspec.toml"
        toml.parent.mkdir()
        spec = RunSpec(
            __runspec_runnable__="myscript",
            __runspec_source__=toml,
        )
        assert spec.runspec_prefix == tmp_path / "mypkg"

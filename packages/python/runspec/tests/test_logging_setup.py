"""Tests for logging_setup.py — stdlib logging configuration from [config.logging]."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from pathlib import Path

import pytest

import runspec.logging_setup as ls
from runspec.logging_setup import (
    _HumanFormatter,
    _JsonFormatter,
    _make_file_handler,
    _resolve_log_dir,
    _SensitiveFilter,
    configure_logging,
)


def _our_console_handlers():
    """Return console handlers added by our code (identified by _HumanFormatter)."""
    return [h for h in logging.getLogger().handlers if isinstance(getattr(h, "formatter", None), _HumanFormatter)]


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
        if isinstance(getattr(h, "formatter", None), (_JsonFormatter, _HumanFormatter)):
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
        configure_logging(None, agent=False, runnable_name="x", config_path=tmp_path / "runspec.toml")
        assert len(logging.getLogger().handlers) == before

    def test_idempotent_second_call_ignored(self, tmp_path):
        cfg = {"level": "info", "rotate": "midnight", "keep": 7}
        toml = tmp_path / "runspec.toml"
        configure_logging(cfg, agent=False, runnable_name="x", config_path=toml)
        first_our_count = len(_our_file_handlers()) + len(_our_console_handlers())
        configure_logging(cfg, agent=False, runnable_name="x", config_path=toml)
        assert len(_our_file_handlers()) + len(_our_console_handlers()) == first_our_count


# ── TestConsoleHuman ──────────────────────────────────────────────────────────


class TestConsoleHuman:
    def _cfg(self, level="info"):
        return {"level": level, "rotate": "midnight", "keep": 7}

    def test_console_handler_added_when_not_agent(self, tmp_path):
        configure_logging(self._cfg(), agent=False, runnable_name="x", config_path=tmp_path / "runspec.toml")
        assert len(_our_console_handlers()) == 1

    def test_info_level_hides_traceback(self, tmp_path, capsys):
        configure_logging(self._cfg("info"), agent=False, runnable_name="x", config_path=tmp_path / "runspec.toml")
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("test.human.info").error("something went wrong", exc_info=True)
        out = capsys.readouterr().err
        assert "Traceback" not in out
        assert "something went wrong" in out

    def test_debug_level_shows_traceback(self, tmp_path, capsys):
        configure_logging(self._cfg("debug"), agent=False, runnable_name="x", config_path=tmp_path / "runspec.toml")
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("test.human.debug").error("oops", exc_info=True)
        out = capsys.readouterr().err
        assert "Traceback" in out

    def test_debug_record_includes_location(self, tmp_path, capsys):
        configure_logging(self._cfg("debug"), agent=False, runnable_name="x", config_path=tmp_path / "runspec.toml")
        logging.getLogger("test.location").debug("at location")
        out = capsys.readouterr().err
        assert ".py:" in out

    def test_format_has_timestamp_and_level(self, tmp_path, capsys):
        configure_logging(self._cfg("info"), agent=False, runnable_name="x", config_path=tmp_path / "runspec.toml")
        logging.getLogger("test.fmt").info("hello world")
        out = capsys.readouterr().err
        assert re.search(r"\d{2}:\d{2}:\d{2}", out)
        assert "INFO" in out


# ── TestAgentMode ─────────────────────────────────────────────────────────────


class TestAgentMode:
    def _cfg(self):
        return {"level": "info", "rotate": "midnight", "keep": 7}

    def test_no_console_handler_in_agent_mode(self, tmp_path):
        configure_logging(self._cfg(), agent=True, runnable_name="x", config_path=tmp_path / "runspec.toml")
        assert _our_console_handlers() == []

    def test_file_handler_present_in_agent_mode(self, tmp_path):
        configure_logging(self._cfg(), agent=True, runnable_name="x", config_path=tmp_path / "runspec.toml")
        assert len(_our_file_handlers()) == 1


# ── TestFileLogging ───────────────────────────────────────────────────────────


class TestFileLogging:
    def _cfg(self, rotate="midnight", keep=7):
        return {"level": "info", "rotate": rotate, "keep": keep}

    def test_file_created_in_logs_subdir(self, tmp_path):
        toml = tmp_path / "pkg" / "runspec.toml"
        toml.parent.mkdir()
        configure_logging(self._cfg(), agent=False, runnable_name="myscript", config_path=toml)
        assert (tmp_path / "pkg" / "logs" / "myscript.log").exists()

    def test_file_content_is_json_lines(self, tmp_path):
        toml = tmp_path / "pkg" / "runspec.toml"
        toml.parent.mkdir()
        configure_logging(self._cfg(), agent=False, runnable_name="myscript", config_path=toml)
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
        configure_logging(self._cfg(), agent=False, runnable_name="myscript", config_path=toml)
        logging.getLogger("test.file.debug").debug("debug message")
        for h in _our_file_handlers():
            h.flush()
        log_file = tmp_path / "pkg" / "logs" / "myscript.log"
        lines = [json.loads(line) for line in log_file.read_text().strip().splitlines()]
        assert any(obj["level"] == "DEBUG" for obj in lines)

    def test_exc_field_in_json_on_exception(self, tmp_path):
        toml = tmp_path / "pkg" / "runspec.toml"
        toml.parent.mkdir()
        configure_logging(self._cfg(), agent=False, runnable_name="myscript", config_path=toml)
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


class TestLogLevelOverride:
    def test_override_raises_console_level(self, tmp_path):
        cfg = {"level": "warning", "rotate": "midnight", "keep": 7}
        configure_logging(cfg, agent=False, runnable_name="x", config_path=tmp_path / "runspec.toml", log_level_override="debug")
        handlers = _our_console_handlers()
        assert handlers[0].level == logging.DEBUG

    def test_override_does_not_affect_file_level(self, tmp_path):
        cfg = {"level": "warning", "rotate": "midnight", "keep": 7}
        configure_logging(cfg, agent=False, runnable_name="x", config_path=tmp_path / "runspec.toml", log_level_override="debug")
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
        cfg = {"level": "info", "rotate": "midnight", "keep": 7}
        configure_logging(cfg, agent=False, runnable_name="x", config_path=tmp_path / "runspec.toml")
        early_logger.info("message from early logger")
        out = capsys.readouterr().err
        assert "message from early logger" in out


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

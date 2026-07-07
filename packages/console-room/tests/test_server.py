"""
test_server.py — the room server's CLI/env config (argparse only; no sockets).
"""

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import console_room.server as server
from console_room.server import (
    DEFAULT_ASSIGNMENT_TIMEOUT,
    _configure_logging,
    _default_log_path,
    _parse_args,
)


def test_assignment_timeout_default():
    assert _parse_args([]).assignment_timeout == DEFAULT_ASSIGNMENT_TIMEOUT


def test_assignment_timeout_flag():
    assert _parse_args(["--assignment-timeout", "30"]).assignment_timeout == 30.0


def test_assignment_timeout_zero_disables():
    assert _parse_args(["--assignment-timeout", "0"]).assignment_timeout == 0.0


def test_assignment_timeout_from_env(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_ASSIGNMENT_TIMEOUT", "45")
    assert _parse_args([]).assignment_timeout == 45.0


# ── logging config ────────────────────────────────────────────────────────────


def test_log_level_defaults_to_info(monkeypatch):
    monkeypatch.delenv("CONSOLE_ROOM_LOG_LEVEL", raising=False)
    assert _parse_args([]).log_level == "info"
    assert _parse_args([]).debug is False


def test_log_level_flag_and_debug_shortcut():
    assert _parse_args(["--log-level", "debug"]).log_level == "debug"
    assert _parse_args(["--debug"]).debug is True


def test_log_level_from_env(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_LOG_LEVEL", "warning")
    assert _parse_args([]).log_level == "warning"


def test_log_file_flag_and_env(monkeypatch, tmp_path):
    monkeypatch.delenv("CONSOLE_ROOM_LOG_FILE", raising=False)
    assert _parse_args(["--log-file", "/tmp/x.log"]).log_file == "/tmp/x.log"
    monkeypatch.setenv("CONSOLE_ROOM_LOG_FILE", str(tmp_path / "y.log"))
    assert _parse_args([]).log_file == str(tmp_path / "y.log")


def _reset_root_logging():
    """Detach handlers added by _configure_logging so a test's file handler
    doesn't leak into later tests (and closes the rotating file)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        try:
            h.close()
        finally:
            root.removeHandler(h)


def test_default_log_path_is_under_sys_prefix():
    import sys

    assert _default_log_path() == Path(sys.prefix) / "logs" / "room.log"


def test_configure_logging_explicit_file_rotates_and_writes(tmp_path):
    log_file = tmp_path / "room.log"
    try:
        warning, resolved = _configure_logging("debug", str(log_file))
        assert warning is None
        assert resolved == str(log_file)
        assert logging.getLogger().level == logging.DEBUG
        # The file handler is a midnight rotator keeping 7 days.
        rotators = [
            h
            for h in logging.getLogger().handlers
            if isinstance(h, TimedRotatingFileHandler)
        ]
        assert len(rotators) == 1
        assert rotators[0].when == "MIDNIGHT" and rotators[0].backupCount == 7
        logging.getLogger("console_room.test").debug("hello-debug")
        for h in logging.getLogger().handlers:
            h.flush()
        assert "hello-debug" in log_file.read_text()
    finally:
        _reset_root_logging()


def test_configure_logging_defaults_to_logs_dir_in_sys_prefix(monkeypatch, tmp_path):
    # With no explicit file, the server creates {sys.prefix}/logs/room.log —
    # point sys.prefix at a tmp dir so the test doesn't touch the real venv.
    monkeypatch.setattr(server.sys, "prefix", str(tmp_path))
    try:
        warning, resolved = _configure_logging("info", None)
        assert warning is None
        assert resolved == str(tmp_path / "logs" / "room.log")
        assert (tmp_path / "logs").is_dir()  # created exist_ok
    finally:
        _reset_root_logging()


def test_configure_logging_bad_file_returns_warning(tmp_path):
    # Make the parent path a *file* so mkdir(parents=True) can't create the dir →
    # a warning string, not an exception (stderr logging still comes up).
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    try:
        warning, resolved = _configure_logging("info", str(blocker / "room.log"))
        assert resolved is None
        assert warning is not None and "could not open log file" in warning
    finally:
        _reset_root_logging()

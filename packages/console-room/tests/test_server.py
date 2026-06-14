"""
test_server.py — the room server's CLI/env config (argparse only; no sockets).
"""

from __future__ import annotations

from console_room.server import DEFAULT_ASSIGNMENT_TIMEOUT, _parse_args


def test_assignment_timeout_default():
    assert _parse_args([]).assignment_timeout == DEFAULT_ASSIGNMENT_TIMEOUT


def test_assignment_timeout_flag():
    assert _parse_args(["--assignment-timeout", "30"]).assignment_timeout == 30.0


def test_assignment_timeout_zero_disables():
    assert _parse_args(["--assignment-timeout", "0"]).assignment_timeout == 0.0


def test_assignment_timeout_from_env(monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_ASSIGNMENT_TIMEOUT", "45")
    assert _parse_args([]).assignment_timeout == 45.0

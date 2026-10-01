"""
test_long_command_line.py — a long arg value no longer breaks a local Windows run.

Windows caps a command line at 32,767 characters (8,191 under ``cmd /c``). A
longer one fails with WinError 206, which Python raises as FileNotFoundError, so a
page-update carrying a whole document body used to surface as "Command not found"
for a binary that exists. ``fit_command_line`` moves the longest values onto the
``RUNSPEC_<RUNNABLE>_ARG_<NAME>`` env channel until the line fits, and
``start_error`` reports the real OS error when a start still fails.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from runspec_console.executor import (
    WIN_CMDLINE_MAX,
    _stream,
    args_to_argv,
    fit_command_line,
    start_error,
)

PREFIX = [r"C:\venv\Scripts\confluence.exe", "update-page"]


def _line(argv: list[str]) -> int:
    return len(subprocess.list2cmdline([*PREFIX, *argv]))


def test_no_budget_is_plain_argv():
    args = {"page": "1", "body": "x" * 50_000, "minor": True}
    argv, env = fit_command_line("confluence", PREFIX, args, None, None)
    assert argv == args_to_argv(args)
    assert env == {}


def test_short_line_untouched():
    args = {"page": "1", "body": "hello"}
    argv, env = fit_command_line("confluence", PREFIX, args, None, 1000)
    assert argv == args_to_argv(args)
    assert env == {}


def test_long_value_moves_to_env_channel():
    body = "<p>" + "x" * 40_000 + "</p>"
    args = {"page-id": "123", "body": body, "minor-edit": True, "label": ["a", "b"]}
    argv, env = fit_command_line("confluence", PREFIX, args, None, 1000)
    # The top-level runnable name keys the channel, even for a subcommand arg.
    assert env == {"RUNSPEC_CONFLUENCE_ARG_BODY": body}
    assert argv == ["--page-id", "123", "--minor-edit", "--label", "a", "--label", "b"]
    assert _line(argv) <= 1000


def test_moves_longest_first_and_only_what_it_needs():
    args = {"a": "x" * 600, "b": "y" * 300, "c": "z" * 10}
    argv, env = fit_command_line("t", PREFIX, args, None, 800)
    assert list(env) == ["RUNSPEC_T_ARG_A"]
    assert "--b" in argv and "--c" in argv


def test_flags_and_lists_never_move():
    args = {"tags": ["x" * 5000], "force": True}
    argv, env = fit_command_line("t", PREFIX, args, None, 100)
    assert env == {}  # nothing movable: left for the OS to reject with a clear error
    assert argv == args_to_argv(args)


def test_positional_moves_only_from_the_end():
    # Moving `first` would shift `second` into position 1 — only the last
    # positional still on argv may leave it.
    positionals = {"first": 1, "second": 2}
    args = {"first": "f" * 5000, "second": "s"}
    argv, env = fit_command_line("t", PREFIX, args, positionals, 1000)
    # So the short trailing one goes first, then the long one (now last).
    assert list(env) == ["RUNSPEC_T_ARG_SECOND", "RUNSPEC_T_ARG_FIRST"]
    assert argv == []
    # With the long one last, it moves and the earlier positional stays put.
    args = {"first": "f", "second": "s" * 5000}
    argv, env = fit_command_line("t", PREFIX, args, positionals, 1000)
    assert env == {"RUNSPEC_T_ARG_SECOND": "s" * 5000}
    assert argv == ["f"]


def test_start_error_missing_program():
    exc = FileNotFoundError(2, "No such file or directory")
    msg = start_error(["/no/such/dir/tool"], exc)
    assert msg == "✗  Command not found: /no/such/dir/tool"


def test_start_error_existing_program_reports_os_error(tmp_path):
    exe = tmp_path / "tool.exe"
    exe.write_text("")
    exc = FileNotFoundError(2, "The filename or extension is too long")
    exc.winerror = 206  # type: ignore[attr-defined]
    msg = start_error([str(exe), "--body", "x" * 40_000], exc)
    assert "Command not found" not in msg
    assert "The filename or extension is too long" in msg
    assert f"Windows allows at most {WIN_CMDLINE_MAX:,}" in msg


def test_start_error_other_os_error(tmp_path):
    exe = tmp_path / "tool"
    exe.write_text("")
    msg = start_error([str(exe)], PermissionError(13, "Permission denied"))
    assert msg.startswith(f"✗  Could not start {exe}: ")
    assert "Permission denied" in msg


def _run(cmd, extra_env=None):
    lines: list[str] = []
    done: list[int] = []
    _stream(
        cmd,
        lambda line, stream: lines.append(line),
        lambda code, ms: done.append(code),
        extra_env=extra_env,
    )
    return lines, done


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command-line limit")
def test_windows_too_long_line_is_reported_not_command_not_found():
    lines, done = _run([sys.executable, "-c", "pass", "x" * 40_000])
    assert done == [-1]
    assert "Command not found" not in lines[0]
    assert "Windows allows at most" in lines[0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows env block size")
def test_windows_env_carries_a_value_too_long_for_the_command_line():
    # The env channel the long values move onto has no 32,767 cap on Windows.
    code = "import os; print(len(os.environ['RUNSPEC_T_ARG_BODY']))"
    lines, done = _run(
        [sys.executable, "-c", code], extra_env={"RUNSPEC_T_ARG_BODY": "x" * 200_000}
    )
    assert done == [0], lines
    assert lines[-1] == "200000"

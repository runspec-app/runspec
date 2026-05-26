"""Tests for runspec.jump — pure helper functions (no SSH required)."""

from __future__ import annotations

import pytest

from runspec.jump import parse_tool_argv, ssh_cmd
from runspec.parser import _parse_argv

# ── ssh_cmd ───────────────────────────────────────────────────────────────────


def test_ssh_cmd_minimal() -> None:
    cmd = ssh_cmd("myserver", "runspec")
    assert cmd == ["ssh", "-o", "BatchMode=yes", "myserver", "runspec", "serve"]


def test_ssh_cmd_with_user_at_host() -> None:
    cmd = ssh_cmd("deploy@myserver", "runspec")
    assert "deploy@myserver" in cmd


def test_ssh_cmd_ends_with_serve() -> None:
    assert ssh_cmd("h", "runspec")[-1] == "serve"


def test_ssh_cmd_full_path_bin() -> None:
    cmd = ssh_cmd("myserver", "/opt/venv/bin/runspec")
    assert "/opt/venv/bin/runspec" in cmd


def test_ssh_cmd_bin_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """RUNSPEC_JUMP_BIN env var supplies the bin path when no --bin flag given."""
    from runspec.jump import _resolve_bin

    monkeypatch.setenv("RUNSPEC_JUMP_BIN", "/opt/runspec/bin/runspec")
    bin_path = _resolve_bin(None)
    assert bin_path == "/opt/runspec/bin/runspec"


def test_ssh_cmd_bin_flag_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from runspec.jump import _resolve_bin

    monkeypatch.setenv("RUNSPEC_JUMP_BIN", "/env/runspec")
    bin_path = _resolve_bin("/flag/runspec")
    assert bin_path == "/flag/runspec"


def test_ssh_cmd_bin_default_when_no_flag_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from runspec.jump import _resolve_bin

    monkeypatch.delenv("RUNSPEC_JUMP_BIN", raising=False)
    bin_path = _resolve_bin(None)
    assert bin_path == "runspec"


# ── _parse_argv against the jump command's spec ───────────────────────────────


_JUMP_SPEC = {
    "host": {"name": "host", "type": "str", "position": 1, "required": False},
    "tool": {"name": "tool", "type": "str", "position": 2, "required": False},
    "bin": {"name": "bin", "type": "str", "required": False},
    "format": {"name": "format", "type": "choice", "options": ["text", "json"], "default": "text"},
    "tool-args": {"name": "tool-args", "type": "rest", "default": []},
}


def test_jump_argv_host_and_tool() -> None:
    result = _parse_argv(["user@prod.example.com", "deploy"], _JUMP_SPEC)
    assert result["host"] == "user@prod.example.com"
    assert result["tool"] == "deploy"


def test_jump_argv_host_only() -> None:
    result = _parse_argv(["myserver"], _JUMP_SPEC)
    assert result["host"] == "myserver"
    assert result["tool"] is None


def test_jump_argv_with_format_flag() -> None:
    result = _parse_argv(["myserver", "--format", "json"], _JUMP_SPEC)
    assert result["host"] == "myserver"
    assert result["format"] == "json"


def test_jump_argv_with_bin_flag() -> None:
    result = _parse_argv(["myserver", "--bin", "/opt/venv/bin/runspec"], _JUMP_SPEC)
    assert result["host"] == "myserver"
    assert result["bin"] == "/opt/venv/bin/runspec"


def test_jump_argv_host_tool_with_rest_args() -> None:
    result = _parse_argv(["myserver", "deploy", "--", "--env", "prod"], _JUMP_SPEC)
    assert result["host"] == "myserver"
    assert result["tool"] == "deploy"
    assert result["tool_args"] == ["--env", "prod"]


def test_jump_argv_no_args() -> None:
    result = _parse_argv([], _JUMP_SPEC)
    assert result["host"] is None
    assert result["tool"] is None


# ── parse_tool_argv ───────────────────────────────────────────────────────────


_SCHEMA = {
    "inputSchema": {
        "properties": {
            "env": {"type": "string", "description": "Environment"},
            "delete": {"type": "boolean", "description": "Delete flag"},
            "count": {"type": "integer", "description": "Count"},
        }
    }
}


def test_parse_tool_argv_string_arg() -> None:
    result = parse_tool_argv(["--env", "prod"], _SCHEMA)
    assert result == {"env": "prod"}


def test_parse_tool_argv_bool_flag() -> None:
    result = parse_tool_argv(["--delete"], _SCHEMA)
    assert result == {"delete": True}


def test_parse_tool_argv_mixed() -> None:
    result = parse_tool_argv(["--env", "prod", "--delete", "--count", "5"], _SCHEMA)
    assert result["env"] == "prod"
    assert result["delete"] is True
    assert result["count"] == "5"


def test_parse_tool_argv_unknown_arg_treated_as_value() -> None:
    result = parse_tool_argv(["--unknown", "val"], _SCHEMA)
    assert result == {"unknown": "val"}


def test_parse_tool_argv_empty() -> None:
    assert parse_tool_argv([], _SCHEMA) == {}


def test_parse_tool_argv_non_flag_tokens_ignored() -> None:
    result = parse_tool_argv(["positional", "--env", "prod"], _SCHEMA)
    assert result == {"env": "prod"}


def test_parse_tool_argv_missing_value_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        parse_tool_argv(["--env"], _SCHEMA)


# ── _report_remote_failure — friendly errors when SSH dies early ─────────────


class _FakeProc:
    """Minimal stand-in for subprocess.Popen used by _report_remote_failure tests."""

    def __init__(self, exit_code: int | None):
        self._exit_code = exit_code

    def wait(self, timeout: float | None = None) -> int:
        if self._exit_code is None:
            import subprocess as _sp

            raise _sp.TimeoutExpired(cmd="ssh", timeout=timeout or 1)
        return self._exit_code


def test_report_remote_failure_bare_name_blames_path(capsys: pytest.CaptureFixture[str]) -> None:
    """exit 127 with bare-name `bin` → 'not on remote PATH' message."""
    from runspec.jump import _report_remote_failure

    with pytest.raises(SystemExit) as exc:
        _report_remote_failure(_FakeProc(exit_code=127), bin_path="runspec")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "exit 127" in err
    assert "PATH" in err
    assert "RUNSPEC_JUMP_BIN" in err


def test_report_remote_failure_absolute_path_blames_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    """exit 127 with explicit `/path/to/runspec` → path hint in message."""
    from runspec.jump import _report_remote_failure

    with pytest.raises(SystemExit) as exc:
        _report_remote_failure(_FakeProc(exit_code=127), bin_path="/nonexistent/.venv/bin/runspec")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "exit 127" in err
    assert "/nonexistent/.venv/bin/runspec" in err
    assert "verify the path exists" in err
    assert "remote shell's PATH" not in err


def test_report_remote_failure_no_bin_path_falls_back_to_path_branch(capsys: pytest.CaptureFixture[str]) -> None:
    from runspec.jump import _report_remote_failure

    with pytest.raises(SystemExit) as exc:
        _report_remote_failure(_FakeProc(exit_code=127))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "exit 127" in err
    assert "PATH" in err


def test_report_remote_failure_ssh_connection(capsys: pytest.CaptureFixture[str]) -> None:
    from runspec.jump import _report_remote_failure

    with pytest.raises(SystemExit) as exc:
        _report_remote_failure(_FakeProc(exit_code=255))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "SSH connection failed" in err


def test_report_remote_failure_stdout_closed_but_process_alive(capsys: pytest.CaptureFixture[str]) -> None:
    from runspec.jump import _report_remote_failure

    with pytest.raises(SystemExit) as exc:
        _report_remote_failure(_FakeProc(exit_code=None))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "closed stdout unexpectedly" in err


# ── _validate_bin_path — lock `bin` to the runspec executable ────────────────


def test_bin_path_default_runspec_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    from runspec.jump import _resolve_bin

    monkeypatch.delenv("RUNSPEC_JUMP_BIN", raising=False)
    assert _resolve_bin("runspec") == "runspec"


def test_bin_path_full_path_to_runspec_accepted() -> None:
    from runspec.jump import _resolve_bin

    assert _resolve_bin("/opt/svc-a/.venv/bin/runspec") == "/opt/svc-a/.venv/bin/runspec"


def test_bin_path_runspec_exe_accepted() -> None:
    from runspec.jump import _resolve_bin

    assert _resolve_bin("C:/Users/dev/.venv/Scripts/runspec.exe") == "C:/Users/dev/.venv/Scripts/runspec.exe"


def test_bin_path_redirection_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    from runspec.jump import _resolve_bin

    with pytest.raises(SystemExit) as exc:
        _resolve_bin("/usr/bin/cat")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "runspec" in err
    assert "/usr/bin/cat" in err
    assert "cat" in err


def test_bin_path_rejected_message_explains_lock(capsys: pytest.CaptureFixture[str]) -> None:
    from runspec.jump import _resolve_bin

    with pytest.raises(SystemExit):
        _resolve_bin("/some/other/tool")
    err = capsys.readouterr().err
    assert "locked" in err.lower() or "cannot be redirected" in err.lower()


def test_bin_path_env_var_also_validated(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """RUNSPEC_JUMP_BIN goes through the same basename check."""
    from runspec.jump import _resolve_bin

    monkeypatch.setenv("RUNSPEC_JUMP_BIN", "/etc/passwd")
    with pytest.raises(SystemExit) as exc:
        _resolve_bin(None)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "/etc/passwd" in err

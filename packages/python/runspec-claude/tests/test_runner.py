"""Tests for the pure runner logic (no `claude` binary or runspec parsing)."""

from __future__ import annotations

import json
import subprocess

import pytest

from runspec_claude.runner import (
    ClaudeError,
    ClaudeNotFoundError,
    _build_command,
    _normalise_result,
    encode_project_dir,
    list_sessions,
    run_claude,
)


def test_build_command_ask_minimal():
    cmd = _build_command(
        prompt="hello",
        mode="ask",
        session=None,
        model=None,
        add_dir=None,
        permission_mode=None,
        allowed_tools=None,
        max_turns=None,
        append_system_prompt=None,
        bare=False,
        claude_bin="claude",
    )
    assert cmd == ["claude", "-p", "hello", "--output-format", "json"]


def test_build_command_all_flags():
    cmd = _build_command(
        prompt="do it",
        mode="ask",
        session=None,
        model="opus",
        add_dir="/extra",
        permission_mode="acceptEdits",
        allowed_tools="Read,Bash",
        max_turns=5,
        append_system_prompt="be terse",
        bare=True,
        claude_bin="claude",
    )
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "opus"
    assert "--add-dir" in cmd and cmd[cmd.index("--add-dir") + 1] == "/extra"
    assert "--permission-mode" in cmd and cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert "--allowedTools" in cmd and cmd[cmd.index("--allowedTools") + 1] == "Read,Bash"
    assert "--max-turns" in cmd and cmd[cmd.index("--max-turns") + 1] == "5"
    assert "--append-system-prompt" in cmd
    assert "--bare" in cmd


def test_build_command_continue_and_resume():
    cont = _build_command(
        prompt="more",
        mode="continue",
        session=None,
        model=None,
        add_dir=None,
        permission_mode=None,
        allowed_tools=None,
        max_turns=None,
        append_system_prompt=None,
        bare=False,
        claude_bin="claude",
    )
    assert "--continue" in cont

    res = _build_command(
        prompt="more",
        mode="resume",
        session="abc123",
        model=None,
        add_dir=None,
        permission_mode=None,
        allowed_tools=None,
        max_turns=None,
        append_system_prompt=None,
        bare=False,
        claude_bin="claude",
    )
    assert res[res.index("--resume") + 1] == "abc123"


def test_build_command_resume_requires_session():
    with pytest.raises(ClaudeError, match="session id"):
        _build_command(
            prompt="x",
            mode="resume",
            session=None,
            model=None,
            add_dir=None,
            permission_mode=None,
            allowed_tools=None,
            max_turns=None,
            append_system_prompt=None,
            bare=False,
            claude_bin="claude",
        )


def test_max_turns_zero_is_uncapped():
    cmd = _build_command(
        prompt="x",
        mode="ask",
        session=None,
        model=None,
        add_dir=None,
        permission_mode=None,
        allowed_tools=None,
        max_turns=0,
        append_system_prompt=None,
        bare=False,
        claude_bin="claude",
    )
    assert "--max-turns" not in cmd


def test_normalise_result_picks_known_keys():
    raw = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 3,
        "result": "done",
        "session_id": "s1",
        "total_cost_usd": 0.01,
        "extra": "ignored-at-top-level",
    }
    out = _normalise_result(json.dumps(raw))
    assert out["result"] == "done"
    assert out["session_id"] == "s1"
    assert out["is_error"] is False
    assert out["raw"]["extra"] == "ignored-at-top-level"


def test_normalise_result_falls_back_to_last_json_line():
    stream = '{"type":"system"}\nnot json\n{"result":"final","session_id":"s2"}\n'
    out = _normalise_result(stream)
    assert out["result"] == "final"
    assert out["session_id"] == "s2"


def test_normalise_result_empty_raises():
    with pytest.raises(ClaudeError, match="no output"):
        _normalise_result("   ")


def test_normalise_result_unparseable_raises():
    with pytest.raises(ClaudeError, match="could not parse"):
        _normalise_result("definitely not json at all")


def test_encode_project_dir():
    assert encode_project_dir("/Users/me/proj") == "-Users-me-proj"
    assert encode_project_dir("/a.b/c_d") == "-a-b-c-d"


def test_run_claude_missing_binary(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ClaudeNotFoundError):
        run_claude("hi", claude_bin="claude")


def test_run_claude_parses_stdout(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = '{"result":"ok","session_id":"s9","is_error":false}'
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc())
    out = run_claude("hi")
    assert out["result"] == "ok"
    assert out["session_id"] == "s9"


def test_run_claude_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ClaudeError, match="timed out"):
        run_claude("hi", timeout=1)


def test_run_claude_nonzero_no_output(monkeypatch):
    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "auth failed"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc())
    with pytest.raises(ClaudeError, match="auth failed"):
        run_claude("hi")


def test_list_sessions_empty_when_missing(tmp_path):
    sessions = list_sessions(cwd=str(tmp_path / "nope"), config_dir=tmp_path / "cfg")
    assert sessions == []


def test_list_sessions_reads_and_sorts(tmp_path):
    cwd = "/work/project"
    config_dir = tmp_path / "claude"
    project_dir = config_dir / "projects" / encode_project_dir(cwd)
    project_dir.mkdir(parents=True)

    old = project_dir / "old-session.jsonl"
    old.write_text(
        json.dumps({"type": "user", "message": {"content": "first question"}}) + "\n",
        encoding="utf-8",
    )
    new = project_dir / "new-session.jsonl"
    new.write_text(
        json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "newer question"}]}}) + "\n",
        encoding="utf-8",
    )
    # Make `new` more recent.
    import os

    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))

    sessions = list_sessions(cwd=cwd, config_dir=config_dir)
    assert [s["session_id"] for s in sessions] == ["new-session", "old-session"]
    assert sessions[0]["title"] == "newer question"
    assert sessions[1]["title"] == "first question"


def test_list_sessions_limit(tmp_path):
    cwd = "/work/x"
    config_dir = tmp_path / "claude"
    project_dir = config_dir / "projects" / encode_project_dir(cwd)
    project_dir.mkdir(parents=True)
    for i in range(5):
        (project_dir / f"s{i}.jsonl").write_text("{}\n", encoding="utf-8")

    sessions = list_sessions(cwd=cwd, config_dir=config_dir, limit=2)
    assert len(sessions) == 2

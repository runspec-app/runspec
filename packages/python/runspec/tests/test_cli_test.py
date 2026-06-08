"""
Tests for the `runspec test` command (cli.cmd_test).

The command iterates discovered runnables and, per runnable, runs an in-process
spec smoke (ParseHarness.smoke) plus a subprocess that executes the entry point
with --help. Both the discovery and the subprocess are patched here so the tests
are deterministic and don't depend on what's installed.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from runspec import cli

# ── helpers ─────────────────────────────────────────────────────────────────

_GOOD_TOML = textwrap.dedent("""\
    [greet]
    description = "Greet someone"

    [greet.args]
    name = {type = "str", required = false, default = "world"}
""")


def _write_toml(tmp_path: Path, content: str = _GOOD_TOML) -> Path:
    p = tmp_path / "runspec.toml"
    p.write_text(content, encoding="utf-8")
    return p


def _item(source: Path, runnable: str) -> dict[str, Any]:
    return {"source": str(source), "runnable": runnable, "spec": {}}


def _ok_proc(returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def _run(items, argv, *, proc=None, find_ep=Path("/fake/bin/greet")):
    """Invoke cmd_test with discovery + subprocess patched. Returns the exit code
    (None if it didn't exit) — caller asserts on it."""
    proc = proc if proc is not None else _ok_proc(0)
    with (
        patch.object(cli, "_discover_installed", return_value=items),
        patch.object(cli, "_find_entry_point", return_value=find_ep),
        patch.object(subprocess, "run", return_value=proc) as _,
    ):
        try:
            cli.cmd_test(argv)
        except SystemExit as e:
            return e.code
    return None


# ── all-pass / exit codes ─────────────────────────────────────────────────────


def test_all_pass_exits_zero(tmp_path, capsys):
    toml = _write_toml(tmp_path)
    code = _run([_item(toml, "greet")], [])
    out = capsys.readouterr().out
    assert code is None  # no SystemExit on success
    assert "1 passed, 0 failed" in out
    assert "✓" in out


def test_no_runnables_exits_zero(tmp_path, capsys):
    code = _run([], [])
    out = capsys.readouterr().out
    assert code is None
    assert "No runspec-aware runnables found" in out


# ── failure modes ─────────────────────────────────────────────────────────────


def test_entry_point_nonzero_fails(tmp_path, capsys):
    toml = _write_toml(tmp_path)
    code = _run([_item(toml, "greet")], [], proc=_ok_proc(1, stderr="ModuleNotFoundError: no boto3"))
    out = capsys.readouterr().out
    assert code == 1
    assert "spec ok, --help failed" in out
    assert "ModuleNotFoundError: no boto3" in out  # last stderr line surfaced


def test_entry_point_missing_fails(tmp_path, capsys):
    toml = _write_toml(tmp_path)
    code = _run([_item(toml, "greet")], [], find_ep=None)
    out = capsys.readouterr().out
    assert code == 1
    assert "entry point not installed" in out


def test_timeout_fails_without_crashing(tmp_path, capsys):
    toml = _write_toml(tmp_path)
    with (
        patch.object(cli, "_discover_installed", return_value=[_item(toml, "greet")]),
        patch.object(cli, "_find_entry_point", return_value=Path("/fake/bin/greet")),
        patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="greet", timeout=30)),
        pytest.raises(SystemExit) as exc,
    ):
        cli.cmd_test([])
    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert "timed out" in out


def test_spec_smoke_failure_fails(tmp_path, capsys):
    """A discovered runnable whose name isn't in its TOML makes ParseHarness
    construction raise — exercising the spec-smoke failure branch."""
    toml = _write_toml(tmp_path)
    code = _run([_item(toml, "ghost")], [])  # 'ghost' not declared in the toml
    out = capsys.readouterr().out
    assert code == 1
    assert "spec failed" in out


# ── isolation: one bad runnable must not abort the others ──────────────────────


def test_one_bad_runnable_does_not_abort_others(tmp_path, capsys):
    toml = _write_toml(tmp_path)
    items = [_item(toml, "ghost"), _item(toml, "greet")]  # first fails spec, second good
    code = _run(items, [])
    out = capsys.readouterr().out
    assert code == 1
    assert "ghost" in out and "greet" in out
    assert "1 passed, 1 failed (2 total)" in out


# ── JSON output ────────────────────────────────────────────────────────────────


def test_json_format_shape(tmp_path, capsys):
    toml = _write_toml(tmp_path)
    _run([_item(toml, "greet")], ["--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"results", "summary"}
    assert payload["summary"] == {"total": 1, "passed": 1, "failed": 0}
    result = payload["results"][0]
    assert result["runnable"] == "greet"
    assert result["ok"] is True
    assert set(result["checks"]) == {"spec_smoke", "entry_point"}


def test_json_failure_still_prints_and_exits_one(tmp_path, capsys):
    toml = _write_toml(tmp_path)
    with (
        patch.object(cli, "_discover_installed", return_value=[_item(toml, "greet")]),
        patch.object(cli, "_find_entry_point", return_value=None),
        patch.object(subprocess, "run", return_value=_ok_proc(0)),
        pytest.raises(SystemExit) as exc,
    ):
        cli.cmd_test(["--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert exc.value.code == 1
    assert payload["summary"]["failed"] == 1


# ── --runnable filter ──────────────────────────────────────────────────────────


def test_runnable_filter(tmp_path, capsys):
    content = textwrap.dedent("""\
        [greet]
        description = "Greet"
        [farewell]
        description = "Bye"
    """)
    toml = _write_toml(tmp_path, content)
    _run([_item(toml, "greet"), _item(toml, "farewell")], ["--runnable", "greet"])
    out = capsys.readouterr().out
    assert "1 passed" in out  # only one tested
    # the filtered-out runnable should not appear as a tested line
    assert "farewell" not in out


# ── _isolated_process_state restores global state ──────────────────────────────


def test_isolated_process_state_restores_environ_and_excepthook():
    import os
    import sys

    sentinel_hook = sys.excepthook
    os.environ["RUNSPEC_TEST_SENTINEL"] = "1"
    try:
        with cli._isolated_process_state():
            os.environ["RUNSPEC_TEST_LEAK"] = "leaked"
            os.environ.pop("RUNSPEC_TEST_SENTINEL", None)
            sys.excepthook = lambda *a: None
        # after the block: leak gone, sentinel restored, excepthook restored
        assert "RUNSPEC_TEST_LEAK" not in os.environ
        assert os.environ.get("RUNSPEC_TEST_SENTINEL") == "1"
        assert sys.excepthook is sentinel_hook
    finally:
        os.environ.pop("RUNSPEC_TEST_SENTINEL", None)
        os.environ.pop("RUNSPEC_TEST_LEAK", None)


# ── _find_entry_point ──────────────────────────────────────────────────────────


def test_find_entry_point_locates_in_launcher_dir(tmp_path, monkeypatch):
    import sys

    # Simulate a script whose interpreter (sys.executable) lives elsewhere than
    # the launcher (sys.argv[0]) — the entry point sits next to the launcher.
    launcher_dir = tmp_path / "venvbin"
    launcher_dir.mkdir()
    (launcher_dir / "deploy").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(launcher_dir / "runspec")])

    found = cli._find_entry_point("deploy")
    assert found is not None
    assert found.name == "deploy"


def test_find_entry_point_missing_returns_none(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", [str(tmp_path / "runspec")])
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    assert cli._find_entry_point("nope-not-here") is None

"""
End-to-end tests for runspec serve.

Spins up a real ``runspec serve`` subprocess, exchanges MCP JSON-RPC messages
over stdin/stdout, and asserts on the full pipeline:

    discovery → serve startup → tool dispatch → subprocess execution → MCP response

A minimal installable package is pip-installed at session start and removed at
teardown.  These tests are intentionally slow (subprocess spawn + pip install)
but run in the normal ``pytest`` suite without any extra markers or CI changes.

Covers:
  - tools/list includes the test tool
  - explicit MCP arg flows through to subprocess stdout
  - RUNSPEC_ARG_* env var in the server env reaches the subprocess
  - explicit MCP arg overrides RUNSPEC_ARG_* already in the server env
  - spec default does NOT overwrite RUNSPEC_ARG_* in the server env (regression guard)
  - _meta.runspec block is present on every tools/call response
  - log file is created at {sys.prefix}/logs/{tool_name}.log after a tool call
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

# ── Constants ─────────────────────────────────────────────────────────────────

_PKG_NAME = "runspec_e2e_pkg"
_TOOL_NAME = "runspec_e2e_echo"

# ── Session fixture: build + install the test package ─────────────────────────


@pytest.fixture(scope="session")
def e2e_pkg(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Build a minimal runspec-aware package, pip-install it, yield, then uninstall."""
    src = tmp_path_factory.mktemp("e2e_src")

    pkg_dir = src / _PKG_NAME
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")

    # Script: echo the message arg so tests can assert on stdout
    (pkg_dir / "echo.py").write_text(
        textwrap.dedent("""\
            def main():
                from runspec import parse
                args = parse()
                print(args.message)
        """)
    )

    # runspec.toml inside the package subdirectory — _check_editable_source finds
    # it by iterating subdirectories of the editable install's source dir.
    (pkg_dir / "runspec.toml").write_text(
        textwrap.dedent(f"""\
            [config]
            name = "runspec-e2e"

            [config.logging]
            enabled = true

            [{_TOOL_NAME}]
            description = "E2E test echo tool"
            autonomy    = "autonomous"

            [{_TOOL_NAME}.args]
            message = {{type = "str", description = "Message to echo", default = "spec-default"}}
        """)
    )

    (src / "pyproject.toml").write_text(
        textwrap.dedent(f"""\
            [build-system]
            requires      = ["setuptools>=69", "wheel"]
            build-backend = "setuptools.build_meta"

            [project]
            name         = "{_PKG_NAME}"
            version      = "0.0.1"
            dependencies = ["runspec"]

            [project.scripts]
            {_TOOL_NAME} = "{_PKG_NAME}.echo:main"

            [tool.setuptools.packages.find]
            where   = ["."]
            include = ["{_PKG_NAME}*"]
        """)
    )

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", str(src)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"pip install -e failed:\n{result.stderr}")

    yield src

    subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "-y", _PKG_NAME],
        capture_output=True,
        text=True,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _runspec_bin() -> str:
    return str(Path(sys.executable).parent / "runspec")


def _start_serve(env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [_runspec_bin(), "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env or os.environ.copy(),
    )


def _rpc(proc: subprocess.Popen[str], request: dict[str, Any]) -> dict[str, Any]:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    return json.loads(line)  # type: ignore[no-any-return]


def _initialize(proc: subprocess.Popen[str]) -> dict[str, Any]:
    return _rpc(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
    )


def _stop(proc: subprocess.Popen[str]) -> None:
    try:
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    finally:
        if proc.stdout and not proc.stdout.closed:
            proc.stdout.close()
        if proc.stderr and not proc.stderr.closed:
            proc.stderr.close()


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_initialize_response(e2e_pkg: Any) -> None:
    proc = _start_serve()
    try:
        resp = _initialize(proc)
        assert resp["result"]["protocolVersion"] == "2024-11-05"
        assert "tools" in resp["result"]["capabilities"]
        # Multi-package venv falls back to the venv directory name; just assert non-empty.
        assert resp["result"]["serverInfo"]["name"]
    finally:
        _stop(proc)


def test_tools_list_includes_echo_tool(e2e_pkg: Any) -> None:
    proc = _start_serve()
    try:
        _initialize(proc)
        resp = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = [t["name"] for t in resp["result"]["tools"]]
        assert _TOOL_NAME in tool_names
    finally:
        _stop(proc)


def test_tool_call_explicit_arg(e2e_pkg: Any) -> None:
    """Explicit MCP arg is passed as --message to the subprocess."""
    proc = _start_serve()
    try:
        _initialize(proc)
        resp = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": _TOOL_NAME, "arguments": {"message": "hello-from-test"}},
            },
        )
        assert resp["result"]["isError"] is False
        assert resp["result"]["content"][0]["text"].strip() == "hello-from-test"
    finally:
        _stop(proc)


def test_env_var_flows_to_subprocess(e2e_pkg: Any) -> None:
    """RUNSPEC_ARG_MESSAGE in the server env reaches the subprocess when no MCP arg is given."""
    env = {**os.environ, "RUNSPEC_ARG_MESSAGE": "from-server-env"}
    proc = _start_serve(env=env)
    try:
        _initialize(proc)
        resp = _rpc(
            proc,
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": _TOOL_NAME, "arguments": {}}},
        )
        assert resp["result"]["isError"] is False
        assert resp["result"]["content"][0]["text"].strip() == "from-server-env"
    finally:
        _stop(proc)


def test_explicit_arg_overrides_env_var(e2e_pkg: Any) -> None:
    """Explicit MCP arg wins over a RUNSPEC_ARG_* already set in the server environment."""
    env = {**os.environ, "RUNSPEC_ARG_MESSAGE": "from-server-env"}
    proc = _start_serve(env=env)
    try:
        _initialize(proc)
        resp = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": _TOOL_NAME, "arguments": {"message": "explicit-override"}},
            },
        )
        assert resp["result"]["isError"] is False
        assert resp["result"]["content"][0]["text"].strip() == "explicit-override"
    finally:
        _stop(proc)


def test_spec_default_does_not_overwrite_env_var(e2e_pkg: Any) -> None:
    """Spec default must NOT overwrite a RUNSPEC_ARG_* var set on the server.

    Regression guard for the _args_to_runspec_env bug: injecting spec defaults
    into the subprocess env overwrote operator-set RUNSPEC_ARG_* vars because
    runspec_env is merged after os.environ. With the fix, no default is injected
    when the arg is absent from the MCP call, so the server env var survives.
    """
    env = {**os.environ, "RUNSPEC_ARG_MESSAGE": "operator-value"}
    proc = _start_serve(env=env)
    try:
        _initialize(proc)
        # No "message" in the MCP call — runspec_env must be empty for this arg
        resp = _rpc(
            proc,
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": _TOOL_NAME, "arguments": {}}},
        )
        assert resp["result"]["isError"] is False
        text = resp["result"]["content"][0]["text"].strip()
        assert text == "operator-value", f"Expected 'operator-value', got '{text}' — spec default overwrote the server env var"
    finally:
        _stop(proc)


def test_meta_block_present_on_success(e2e_pkg: Any) -> None:
    """_meta.runspec block is present with correct fields on a successful call."""
    proc = _start_serve()
    try:
        _initialize(proc)
        resp = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": _TOOL_NAME, "arguments": {"message": "meta-test"}},
            },
        )
        meta = resp["result"]["_meta"]["runspec"]
        assert meta["tool"] == _TOOL_NAME
        assert meta["exit_code"] == 0
        assert isinstance(meta["duration_ms"], int)
    finally:
        _stop(proc)


def test_log_file_created_at_sys_prefix(e2e_pkg: Any) -> None:
    """After a tool call, the audit log exists at {sys.prefix}/logs/{tool_name}.log."""
    expected_log = Path(sys.prefix) / "logs" / f"{_TOOL_NAME}.log"
    expected_log.unlink(missing_ok=True)

    proc = _start_serve()
    try:
        _initialize(proc)
        resp = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": _TOOL_NAME, "arguments": {"message": "log-test"}},
            },
        )
        assert resp["result"]["isError"] is False
    finally:
        _stop(proc)

    assert expected_log.exists(), f"Log file not found at {expected_log}"

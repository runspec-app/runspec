"""
jump.py — SSH+MCP client for runspec jump.

Connects to a remote host via SSH subprocess, starts 'runspec serve'
on the remote, and communicates via JSON-RPC stdio (MCP 2024-11-05).

Connection parameters (user, port, key, ProxyJump, etc.) come from
~/.ssh/config — pass a plain SSH connection string like user@host.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any


def _resolve_bin_raw(bin_flag: str | None = None) -> str:
    """Cascade only — no validation. Used when we need to show what *would*
    happen at jump time even if the value is currently invalid."""
    return bin_flag or os.environ.get("RUNSPEC_JUMP_BIN") or "runspec"


def _resolve_bin(bin_flag: str | None = None) -> str:
    """Cascade: --bin CLI flag → RUNSPEC_JUMP_BIN env var → 'runspec' default.

    Validates the result.
    """
    bin_path = _resolve_bin_raw(bin_flag)
    _validate_bin_path(bin_path)
    return bin_path


def ssh_cmd(host: str, bin_path: str) -> list[str]:
    """Build the SSH command list to start runspec serve on the remote.

    BatchMode=yes is locked because runspec jump pipes JSON-RPC over
    stdin/stdout — interactive prompts would corrupt the protocol.
    All other SSH options (user, port, key, ProxyJump, etc.) come from
    ~/.ssh/config for the given host.
    """
    return ["ssh", "-o", "BatchMode=yes", host, bin_path, "serve"]


# Names accepted as the remote runspec executable. Anything else is rejected.
# Locks the `bin` field to its documented purpose (no accidental redirection
# to unrelated binaries via a stale env var).
_VALID_BIN_NAMES = frozenset({"runspec", "runspec.exe"})


def _validate_bin_path(bin_path: str) -> None:
    """Enforce that the remote bin is named `runspec` (or `runspec.exe`)."""
    import os.path

    name = os.path.basename(bin_path)
    if name not in _VALID_BIN_NAMES:
        sys.stderr.write(
            f"✗  Jump `bin` must point at a runspec executable.\n"
            f"   Got: {bin_path!r} (basename {name!r})\n"
            f"   Expected basename: 'runspec' (or 'runspec.exe' on Windows).\n"
            f"   This field is locked to the runspec CLI; it cannot be redirected.\n"
        )
        sys.exit(1)


def _open_session(host: str, bin_path: str) -> subprocess.Popen[bytes]:
    """Open an SSH+MCP session. Exits on connection failure."""
    cmd = ssh_cmd(host, bin_path)
    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
        )
    except FileNotFoundError:
        sys.stderr.write("✗  ssh not found — install OpenSSH\n")
        sys.exit(1)


def _send(proc: subprocess.Popen[bytes], msg: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()


def _recv(proc: subprocess.Popen[bytes], bin_path: str | None = None) -> dict[str, Any]:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        _report_remote_failure(proc, bin_path)
    return json.loads(line.decode())  # type: ignore[no-any-return]


def _report_remote_failure(proc: subprocess.Popen[bytes], bin_path: str | None = None) -> None:
    """The remote produced no MCP response — figure out why and report cleanly."""
    try:
        exit_code = proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        exit_code = None  # still alive but stdout closed — unusual

    if exit_code == 255:
        sys.stderr.write("✗  SSH connection failed (see error above for details).\n")
    elif exit_code is not None and exit_code != 0:
        prefix = f"✗  Remote command failed (exit {exit_code}) before the MCP handshake completed.\n"
        if bin_path and "/" in bin_path:
            sys.stderr.write(
                prefix + "   If the error above doesn't explain it, verify the path exists on the remote:\n"
                f"     {bin_path}\n"
                "   Common causes:\n"
                "     - the venv path differs between local and remote\n"
                "     - runspec isn't installed in that venv on the remote\n"
                "     - typo in the --bin / RUNSPEC_JUMP_BIN value\n"
            )
        else:
            sys.stderr.write(
                prefix + f"   `{bin_path or 'runspec'}` is not on the remote shell's PATH.\n"
                "   Fix: pass --bin /full/path/to/runspec to runspec jump,\n"
                "   or export RUNSPEC_JUMP_BIN in your local shell.\n"
                "   (SSH commands run in a non-login shell and don't source ~/.bashrc / ~/.profile.)\n"
            )
    else:
        sys.stderr.write("✗  Remote MCP server closed stdout unexpectedly\n")
    sys.exit(1)


def _close(proc: subprocess.Popen[bytes]) -> None:
    if proc.stdin:
        proc.stdin.close()
    proc.wait()


def _initialize(proc: subprocess.Popen[bytes], bin_path: str | None = None) -> None:
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "runspec-jump", "version": "1.0"},
            },
        },
    )
    _recv(proc, bin_path)
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})


def list_tools(host: str, bin_path: str) -> list[dict[str, Any]]:
    """List tools available on a remote host via SSH+MCP."""
    proc = _open_session(host, bin_path)
    try:
        _initialize(proc, bin_path)
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        resp = _recv(proc, bin_path)
        return resp.get("result", {}).get("tools", [])  # type: ignore[no-any-return]
    finally:
        _close(proc)


def call_tool(host: str, bin_path: str, tool_name: str, tool_argv: list[str]) -> None:
    """Call a tool on a remote host via SSH+MCP, streaming text output to stdout."""
    proc = _open_session(host, bin_path)
    try:
        _initialize(proc, bin_path)
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_resp = _recv(proc, bin_path)
        tools = tools_resp.get("result", {}).get("tools", [])
        schema = next((t for t in tools if t["name"] == tool_name), None)
        if schema is None:
            sys.stderr.write(f"✗  Tool '{tool_name}' not found on remote\n")
            sys.exit(1)

        arguments = parse_tool_argv(tool_argv, schema)
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        call_resp = _recv(proc, bin_path)
        if "error" in call_resp:
            sys.stderr.write(f"✗  {call_resp['error'].get('message', 'Remote error')}\n")
            sys.exit(1)
        result = call_resp.get("result", {})
        for block in result.get("content", []):
            if block.get("type") == "text":
                text = block["text"]
                sys.stdout.write(text)
                if not text.endswith("\n"):
                    sys.stdout.write("\n")
        if result.get("isError"):
            sys.exit(1)
    finally:
        _close(proc)


def parse_tool_argv(argv: list[str], schema: dict[str, Any]) -> dict[str, Any]:
    """Parse --flag [value] argv into a call arguments dict using the tool's JSON Schema."""
    props = schema.get("inputSchema", {}).get("properties", {})
    result: dict[str, Any] = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if not token.startswith("--"):
            i += 1
            continue
        arg_name = token[2:]
        prop = props.get(arg_name, {})
        if prop.get("type") == "boolean":
            result[arg_name] = True
            i += 1
        elif i + 1 < len(argv):
            result[arg_name] = argv[i + 1]
            i += 2
        else:
            sys.stderr.write(f"✗  --{arg_name} requires a value\n")
            sys.exit(1)
    return result

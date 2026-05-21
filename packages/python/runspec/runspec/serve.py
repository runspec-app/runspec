"""
serve.py — MCP stdio server for runspec.

Implements the Model Context Protocol over stdin/stdout (zero dependencies).
Protocol: https://github.com/modelcontextprotocol/specification
Version:  2024-11-05
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from typing import Any

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_SPEC = "https://github.com/modelcontextprotocol/specification"

# Standard JSON-RPC 2.0 error codes
_ERR_PARSE = -32700
_ERR_METHOD_NOT_FOUND = -32601
_ERR_INVALID_PARAMS = -32602


def serve() -> None:
    """
    Start the runspec MCP stdio server.
    Reads JSON-RPC requests from stdin, writes responses to stdout.
    Runs until stdin closes.

    Discovers runnables via importlib.metadata — the venv is the source of
    truth. Packages must be installed (pip install / pip install -e) to be
    served. Same convention as `runspec local` and `runspec jump`.
    """
    from runspec.cli import _deduplicate, _discover_installed
    from runspec.inference import infer_script
    from runspec.loader import load_raw

    hostname = socket.gethostname()
    scripts_dir = Path(sysconfig.get_path("scripts"))

    discovered = _deduplicate(_discover_installed())
    if not discovered:
        sys.stderr.write("runspec serve: no runspec-aware packages installed in this venv.\n")
        sys.stderr.write("Install one with: pip install -e <path-to-project>\n")
        sys.stderr.flush()
        sys.exit(1)

    # Cache each unique source TOML's [config] so its autonomy-default applies
    # to that package's own runnables — multi-package venvs are honoured per pkg.
    configs_by_source: dict[str, dict[str, Any]] = {}
    for item in discovered:
        src = item["source"]
        if src not in configs_by_source:
            configs_by_source[src] = load_raw(Path(src))["config"]

    tools: dict[str, dict[str, Any]] = {}
    arg_specs: dict[str, dict[str, Any]] = {}
    exec_specs: dict[str, dict[str, Any]] = {}
    seen_runnables: set[str] = set()

    for item in discovered:
        rname = item["runnable"]
        runnable = item["spec"]
        config = configs_by_source[item["source"]]

        # First-wins when the same runnable name appears in two installed packages
        if rname in seen_runnables:
            sys.stderr.write(f"runspec serve: warning: '{rname}' defined in multiple installed packages, keeping first\n")
            sys.stderr.flush()
            continue
        seen_runnables.add(rname)

        # Skip tools restricted to other hosts
        allowed_hosts = runnable.get("hosts")
        if allowed_hosts and hostname not in allowed_hosts:
            continue

        # Validate run_as patterns — exit with a clear error rather than silently misbehaving
        pattern_errors = _validate_run_as_patterns(runnable.get("run_as"))
        if pattern_errors:
            for err in pattern_errors:
                sys.stderr.write(f"runspec serve: {rname}.run_as: {err}\n")
            sys.stderr.flush()
            sys.exit(1)

        inferred = infer_script(runnable, config["autonomy_default"])
        base_cmd = _find_script(rname, scripts_dir)
        run_as = _resolve_run_as(runnable.get("run_as"), hostname)
        become_method = runnable.get("become_method", "sudo")
        become_flags = runnable.get("become_flags")
        config_path = item["source"]  # path to the runspec.toml that defined this runnable

        for flat_name, schema, arg_spec, exec_spec in _expand_tools(rname, inferred, base_cmd, run_as, become_method, become_flags, config_path):
            tools[flat_name] = schema
            arg_specs[flat_name] = arg_spec
            exec_specs[flat_name] = exec_spec

    # Single-package venvs use that package's [config] name; multi-package
    # venvs fall back to the venv directory name to avoid arbitrary choice.
    server_config = next(iter(configs_by_source.values())) if len(configs_by_source) == 1 else {}
    _mcp_loop(tools, arg_specs, exec_specs, _server_name(server_config))


# ── MCP loop ──────────────────────────────────────────────────────────────────


def _mcp_loop(
    tools: dict[str, dict[str, Any]],
    arg_specs: dict[str, dict[str, Any]],
    exec_specs: dict[str, dict[str, Any]],
    server_name: str,
) -> None:
    if sys.stdin.isatty():
        print("runspec serve is an MCP stdio server — it is not run directly from a terminal.")
        print("Configure it as an MCP server in your MCP host (Claude Desktop, VS Code, PyCharm, etc.)")
        print()
        print("To test manually:")
        print('  echo \'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}\' | runspec serve')
        return
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                _write({"jsonrpc": "2.0", "id": None, "error": {"code": _ERR_PARSE, "message": "Parse error"}})
                continue

            response = _dispatch(request, tools, arg_specs, exec_specs, server_name)
            if response is not None:
                _write(response)
    except KeyboardInterrupt:
        pass


def _dispatch(
    request: dict[str, Any],
    tools: dict[str, dict[str, Any]],
    arg_specs: dict[str, dict[str, Any]],
    exec_specs: dict[str, dict[str, Any]],
    server_name: str,
) -> dict[str, Any] | None:
    method = request.get("method", "")
    req_id = request.get("id")

    # Notifications have no id — no response
    if req_id is None:
        return None

    if method == "initialize":
        return _handle_initialize(req_id, server_name)
    if method == "tools/list":
        return _handle_tools_list(req_id, tools)
    if method == "tools/call":
        return _handle_tools_call(req_id, request.get("params", {}), tools, arg_specs, exec_specs)

    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": _ERR_METHOD_NOT_FOUND, "message": f"Method not found: {method}"}}


# ── Handlers ──────────────────────────────────────────────────────────────────


def _handle_initialize(req_id: Any, server_name: str) -> dict[str, Any]:
    from runspec import __version__

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": server_name, "version": __version__},
        },
    }


def _handle_tools_list(req_id: Any, tools: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"tools": list(tools.values())},
    }


def _handle_tools_call(
    req_id: Any,
    params: dict[str, Any],
    tools: dict[str, dict[str, Any]],
    arg_specs: dict[str, dict[str, Any]],
    exec_specs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    name = params.get("name", "")
    arguments = params.get("arguments") or {}

    if name not in tools:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": _ERR_INVALID_PARAMS, "message": f"Unknown tool: {name}"},
        }

    cmd: list[str] | None = exec_specs.get(name, {}).get("command")
    if cmd is None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": f"Script '{name}' not found in the venv. Install it with: pip install -e <path-to-project>"}],
                "isError": True,
            },
        }

    tool_arg_specs = arg_specs.get(name, {})
    argv = _args_to_argv(arguments, tool_arg_specs)

    runspec_env = _args_to_runspec_env(arguments, tool_arg_specs)
    env = {**os.environ, "RUNSPEC_AGENT": "1", **runspec_env}

    # Tell the subprocess where its runspec.toml lives. Otherwise the tool's
    # parse() would walk up from cwd (typically $HOME for SSH-launched serves)
    # and fail to find the spec — even though serve already knew its location.
    config_path = exec_specs.get(name, {}).get("config_path")
    if config_path:
        env["RUNSPEC_CONFIG"] = str(config_path)

    start = time.monotonic()
    result = subprocess.run([*cmd, *argv], capture_output=True, text=True, env=env)
    duration_ms = int((time.monotonic() - start) * 1000)

    # _meta is the MCP-standard extension point; clients that don't understand
    # it ignore the block. Same envelope on success and failure so callers can
    # rely on it being present.
    meta = {"runspec": {"tool": name, "duration_ms": duration_ms, "exit_code": result.returncode}}

    if result.returncode == 0:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": result.stdout}],
                "isError": False,
                "_meta": meta,
            },
        }

    parts = [f"exit_code: {result.returncode}"]
    if result.stdout:
        parts.append(f"stdout:\n{result.stdout.rstrip()}")
    if result.stderr:
        parts.append(f"stderr:\n{result.stderr.rstrip()}")

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": "\n".join(parts)}],
            "isError": True,
            "_meta": meta,
        },
    }


# ── Arg translation ───────────────────────────────────────────────────────────


def _args_to_argv(arguments: dict[str, Any], arg_specs: dict[str, Any]) -> list[str]:
    """Translate MCP tool call arguments dict to a CLI argv list."""
    argv: list[str] = []

    for arg_name, spec in arg_specs.items():
        # Accept both hyphen and underscore forms from the caller
        value = arguments.get(arg_name)
        if value is None:
            value = arguments.get(arg_name.replace("-", "_"))
        if value is None:
            continue

        flag = f"--{arg_name}"
        arg_type = spec.get("type", "str")

        if arg_type == "flag":
            if value:
                argv.append(flag)
        elif spec.get("multiple") and isinstance(value, list):
            for item in value:
                argv.extend([flag, str(item)])
        else:
            argv.extend([flag, str(value)])

    return argv


# ── run_as helpers ────────────────────────────────────────────────────────────


def _resolve_run_as(run_as_spec: Any, hostname: str) -> str:
    """Resolve run_as to a plain string for the current host."""
    if run_as_spec is None:
        return ""

    # Simple string or $ENV_VAR reference
    if isinstance(run_as_spec, str):
        if run_as_spec.startswith("$"):
            return os.environ.get(run_as_spec[1:], "")
        return run_as_spec

    # Table form: hosts / patterns / default
    if isinstance(run_as_spec, dict):
        hosts = run_as_spec.get("hosts", {})
        if hostname in hosts:
            return str(hosts[hostname])

        for pattern, user in run_as_spec.get("patterns", {}).items():
            if re.fullmatch(pattern, hostname):
                return str(user)

        return str(run_as_spec.get("default", ""))

    return ""


def _validate_run_as_patterns(run_as_spec: Any) -> list[str]:
    """Return a list of error messages for any invalid regex patterns."""
    if not isinstance(run_as_spec, dict):
        return []

    errors: list[str] = []
    for pattern in run_as_spec.get("patterns", {}):
        try:
            re.compile(pattern)
        except re.error as e:
            errors.append(f"invalid pattern '{pattern}': {e}")
    return errors


# ── Helpers ───────────────────────────────────────────────────────────────────


def _expand_tools(
    name: str,
    inferred: dict[str, Any],
    base_cmd: list[str] | None,
    run_as: str | None,
    become_method: str,
    become_flags: str | None,
    config_path: str | None = None,
) -> list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Expand a runnable into flat MCP tool entries, recursing into subcommands.

    A runnable with no commands yields one entry (name, schema, arg_spec, exec_spec).
    A runnable with commands yields one entry per leaf subcommand, with underscore-joined
    names and the subcommand path prepended to base_cmd.
    e.g. portal_api.commands.orders_endpoint.commands.get_list →
         name="portal_api_orders_endpoint_get_list",
         base_cmd=["/path/to/portal_api", "orders_endpoint", "get_list"]
    """
    from runspec.cli import _build_schema

    commands = inferred.get("commands") or {}
    if commands:
        result: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for sub_name, sub_spec in commands.items():
            result.extend(
                _expand_tools(
                    f"{name}_{sub_name}",
                    sub_spec,
                    [*(base_cmd or []), sub_name],
                    run_as,
                    become_method,
                    become_flags,
                    config_path,
                )
            )
        return result
    return [
        (
            name,
            _build_schema(name, inferred, "mcp"),
            inferred.get("args", {}),
            {
                "command": base_cmd,
                "run_as": run_as,
                "become_method": become_method,
                "become_flags": become_flags,
                "config_path": config_path,
            },
        )
    ]


def _find_script(name: str, scripts_dir: Path) -> list[str] | None:
    """Return a command list for the named script, or None if not found.

    Looks only in the venv scripts dir — tools must be installed (pip install -e .).
    """
    for ext in ("", ".exe"):
        candidate = scripts_dir / (name + ext)
        if candidate.is_file():
            return [str(candidate)]
    return None


def _server_name(config: dict[str, Any]) -> str:
    """Agent name: explicit config name, or the venv directory name."""
    name = config.get("name")
    if name and isinstance(name, str):
        return name
    return Path(sys.prefix).name


def _write(response: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()


def _arg_name_to_env_key(name: str) -> str:
    """Convert an arg name to its RUNSPEC_* environment variable key."""
    return "RUNSPEC_" + name.upper().replace("-", "_")


def _args_to_runspec_env(arguments: dict[str, Any], arg_specs: dict[str, Any]) -> dict[str, str]:
    """Convert a resolved arguments dict to RUNSPEC_* environment variables."""
    env_vars: dict[str, str] = {}
    for arg_name, spec in arg_specs.items():
        value = arguments.get(arg_name)
        if value is None:
            value = arguments.get(arg_name.replace("-", "_"))
        if value is None:
            value = spec.get("default")
        if value is None:
            continue

        env_key = _arg_name_to_env_key(arg_name)
        arg_type = spec.get("type", "str")

        if arg_type in ("bool", "flag"):
            env_vars[env_key] = "1" if value else "0"
        elif spec.get("multiple") and isinstance(value, list):
            env_vars[env_key] = "\n".join(str(v) for v in value)
        else:
            env_vars[env_key] = str(value)

    return env_vars

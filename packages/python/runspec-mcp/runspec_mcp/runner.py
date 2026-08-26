"""
runner.py — execute a catalog Tool for an MCP tools/call, with credential injection.

Resolves the credentials a runnable needs (auto-matched by its declared arg ``env``
names, or by scope globs), reads their secrets from the keychain, derives the plain
env vars, and runs the runnable via the vendored ``executor`` — locally as a
subprocess or over the pooled SSH transport — collecting stdout/stderr into a
single text result (MCP ``tools/call`` returns one blob, not a stream).
"""

from __future__ import annotations

import threading
from typing import Any

from . import credentials as cred
from . import executor
from .catalog import Tool
from .config import FleetConfig


def resolve_credentials(tool: Tool, cfg: FleetConfig) -> tuple[dict[str, str], dict[str, Any], dict[str, str]]:
    """Resolve credentials for a tool call.

    Returns ``(cred_env, arg_overrides, secret_env)``:
    * ``cred_env`` — plain name-derived env vars (``WINDOWS_USERNAME`` …).
    * ``arg_overrides`` — a matched credential's ``username_arg`` → username (argv).
    * ``secret_env`` — a matched credential's ``secret_arg`` → secret, keyed by the
      runnable arg name so ``executor.secret_env`` maps it to ``RUNSPEC_*_ARG_*``.
    """
    host_name = str(tool.host.get("name") or "")
    entries = cred.match_credentials(tool.arg_env_names, host_name, tool.runnable, cfg.credentials)
    cred_env: dict[str, str] = {}
    arg_overrides: dict[str, Any] = {}
    secrets: dict[str, str] = {}
    for entry in entries:
        cid = str(entry.get("id", ""))
        secret = cred.get_secret(cid) if cid else None
        for k, v in cred.env_vars_for(entry, secret).items():
            cred_env[k] = v
        if entry.get("username_arg") and entry.get("username"):
            arg_overrides[str(entry["username_arg"])] = str(entry["username"])
        if entry.get("secret_arg") and secret:
            secrets[str(entry["secret_arg"])] = secret
    return cred_env, arg_overrides, secrets


def run_tool(tool: Tool, arguments: dict[str, Any], cfg: FleetConfig, pool: Any, timeout: int | None = None) -> tuple[int, str]:
    """Execute ``tool`` with the given MCP arguments; return ``(exit_code, text)``."""
    cred_env, arg_overrides, secrets = resolve_credentials(tool, cfg)
    args: dict[str, Any] = {**(arguments or {}), **arg_overrides}

    lines: list[str] = []
    result: dict[str, int] = {"code": -1, "ms": 0}
    done = threading.Event()

    def on_line(line: str, stream: str) -> None:
        lines.append(line)

    def on_done(code: int, ms: int) -> None:
        result["code"] = code
        result["ms"] = ms
        done.set()

    if tool.is_local:
        executor.run_local(
            tool.runspec_path,
            tool.runnable,
            args,
            tool.command_path,
            on_line,
            on_done,
            timeout=timeout,
            agent=True,
            run_as=tool.run_as,
            become_method=tool.become_method,
            become_flags=tool.become_flags,
            positionals=tool.positionals,
            secrets=secrets,
            cred_env=cred_env,
        )
    else:
        ssh_target = str(tool.host.get("ssh"))
        identity = tool.host.get("identity_file")
        jump = str(tool.host.get("jump") or "")

        def session_factory() -> Any:
            return pool.open_session(ssh_target, identity_file=identity, global_ssh_config=cfg.ssh)

        executor.run_remote(
            ssh_target,
            tool.runspec_path,
            tool.runnable,
            args,
            tool.command_path,
            on_line,
            on_done,
            identity_file=identity,
            timeout=timeout,
            agent=True,
            global_ssh_config=cfg.ssh,
            run_as=tool.run_as,
            become_method=tool.become_method,
            become_flags=tool.become_flags,
            session_factory=None if jump else session_factory,
            positionals=tool.positionals,
            secrets=secrets,
            cred_env=cred_env,
            jump=jump,
        )

    done.wait()
    return result["code"], "\n".join(lines)

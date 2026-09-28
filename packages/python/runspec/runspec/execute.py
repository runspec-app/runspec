"""execute.py — resolve an installed runnable and build the command that runs it.

The execution half of ``runspec serve``'s ``tools/call`` handler, extracted so
``runspec exec`` (docs/design/runspec-exec.md) runs a runnable exactly the way
``serve`` does: the binary from the venv scripts dir, its ``runspec.toml`` passed
as ``RUNSPEC_CONFIG`` (so its ``parse()`` finds the spec whatever the cwd), and
the declared ``run_as`` applied through :mod:`runspec.become`. Pure — nothing
here spawns a process.
"""

from __future__ import annotations

import socket
import sysconfig
from pathlib import Path
from typing import Any

from runspec.become import build_become_argv, resolve_run_as


def find_script(name: str, scripts_dir: Path | None = None) -> list[str] | None:
    """The command list for an installed runnable's binary, or ``None``.

    Looks only in the venv scripts dir — runnables must be installed
    (``pip install`` / ``pip install -e``)."""
    scripts = scripts_dir or Path(sysconfig.get_path("scripts"))
    for ext in ("", ".exe"):
        candidate = scripts / (name + ext)
        if candidate.is_file():
            return [str(candidate)]
    return None


def find_runnable(name: str) -> dict[str, Any] | None:
    """The exec spec for an installed runnable by name, or ``None`` if no
    installed package defines it.

    ``{runnable, command, config_path, run_as, become_method, become_flags,
    config}`` — ``command`` is ``None`` when the package is installed but its
    entry point binary is missing. Resolution matches ``runspec serve``/``local``
    (installed packages via ``importlib.metadata``, first definition wins), but
    unlike ``serve`` it does **not** filter on ``discoverable`` or the ``serve``
    context: hidden is not disabled, and a direct invocation may run any
    installed runnable."""
    from runspec.cli import _deduplicate, _discover_installed
    from runspec.loader import load_raw

    for item in _deduplicate(_discover_installed()):
        if item["runnable"] != name:
            continue
        runnable = item["spec"]
        return {
            "runnable": name,
            "command": find_script(name),
            "config_path": item["source"],
            "run_as": resolve_run_as(runnable.get("run_as"), socket.gethostname()),
            "become_method": runnable.get("become_method", "sudo"),
            "become_flags": runnable.get("become_flags"),
            "config": load_raw(Path(item["source"]))["config"],
        }
    return None


def build_command(
    spec: dict[str, Any],
    argv: list[str],
    env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The final argv and the extra environment to run ``spec`` with ``argv``.

    ``env`` are variables the runnable must see (``RUNSPEC_*`` provenance, the
    secret arg channel, …); ``RUNSPEC_CONFIG`` is added from the spec. Without
    ``run_as`` they're returned for the caller to put in the subprocess
    environment. With ``run_as`` they're placed *inside* the escalated command via
    ``env(1)`` (sudo strips the environment) and also returned, harmlessly."""
    extra = dict(env or {})
    if spec.get("config_path"):
        extra["RUNSPEC_CONFIG"] = str(spec["config_path"])
    cmd = [*(spec.get("command") or []), *argv]
    run_as = spec.get("run_as") or ""
    if run_as:
        cmd = build_become_argv(
            cmd,
            run_as,
            spec.get("become_method", "sudo"),
            spec.get("become_flags"),
            env=extra,
        )
    return cmd, extra

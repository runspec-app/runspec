"""runner.py — locate and execute a runnable installed in this venv.

The daemon fires a schedule by exec'ing the target runnable's own venv script
directly (a sibling of the ``runspec`` binary in the Scripts dir) — the same
approach ``runspec serve`` / the console executor use — never by shelling through
``runspec run``. Credentials, when present, arrive via ``RUNSPEC_ENV_FILE`` (a
600 file owned by ``run_as``), so only non-secret provenance rides ``env(1)``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Any

from runspec.become import build_become_argv


def scripts_dir() -> Path:
    """The venv's Scripts/bin directory (where entry-point binaries live)."""
    return Path(sysconfig.get_path("scripts"))


def find_binary(runnable: str) -> Path | None:
    """Locate a runnable's entry-point binary in this venv, or ``None``."""
    base = scripts_dir()
    for candidate in (base / runnable, base / f"{runnable}.exe"):
        if candidate.exists():
            return candidate
    return None


def args_to_argv(args: dict[str, Any]) -> list[str]:
    """Turn a schedule's ``{name: value}`` args into CLI argv.

    Flags: ``True`` → ``--name``, ``False`` → ``--no-name`` (runspec's negation).
    Everything else → ``--name value``. Keeps the mapping simple and predictable;
    scheduled runs use named args only.
    """
    argv: list[str] = []
    for name, value in args.items():
        flag = f"--{name}"
        if isinstance(value, bool):
            argv.append(flag if value else f"--no-{name}")
        else:
            argv.extend([flag, str(value)])
    return argv


def provenance_env(source_toml: str, env_file: str = "") -> dict[str, str]:
    """The non-secret environment handed to every scheduled run."""
    env = {
        "RUNSPEC_AGENT": "1",
        "RUNSPEC_TRIGGER": "schedule",
    }
    if source_toml:
        # So the child's parse() finds its runspec.toml regardless of cwd.
        env["RUNSPEC_CONFIG"] = source_toml
    if env_file:
        env["RUNSPEC_ENV_FILE"] = env_file
    return env


def run_runnable(
    binary: Path,
    command_path: list[str],
    args: dict[str, Any],
    *,
    source_toml: str = "",
    run_as: str = "",
    become_method: str = "sudo",
    become_flags: str | None = None,
    env_file: str = "",
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute one runnable invocation, escalating to ``run_as`` when set."""
    cmd = [str(binary), *command_path, *args_to_argv(args)]
    prov = provenance_env(source_toml, env_file)
    if run_as:
        # env(1) carries only the non-secret provenance + the RUNSPEC_ENV_FILE
        # path through sudo/su; the credentials themselves live in that file.
        argv = build_become_argv(
            cmd,
            run_as=run_as,
            become_method=become_method,
            become_flags=become_flags,
            env=prov,
        )
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    child_env = {**os.environ, **prov}
    return subprocess.run(cmd, capture_output=True, text=True, env=child_env, timeout=timeout)


def is_generic_venv_name(name: str) -> bool:
    """Whether ``name`` is a non-descriptive venv dir name (venv/.venv/env/…)."""
    return name.strip().lower() in {"venv", ".venv", "env", ".env", "virtualenv", ""}


def venv_name() -> str:
    """This venv's directory name (its runnable-group identity)."""
    return Path(sys.prefix).name

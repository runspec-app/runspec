"""runner.py — where this venv's runnables live.

Both backends fire a schedule through core's ``runspec exec`` (see
:func:`runspec_scheduler.cronsync.exec_invocation`), which resolves and runs
the runnable itself; these helpers only locate the venv's scripts directory and
a runnable's entry-point binary (to check it's installed).
"""

from __future__ import annotations

import sys
import sysconfig
from pathlib import Path


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


def is_generic_venv_name(name: str) -> bool:
    """Whether ``name`` is a non-descriptive venv dir name (venv/.venv/env/…)."""
    return name.strip().lower() in {"venv", ".venv", "env", ".env", "virtualenv", ""}


def venv_name() -> str:
    """This venv's directory name (its runnable-group identity)."""
    return Path(sys.prefix).name

"""
mcp_venv.py — install a pip-distributed stdio MCP server into a dedicated venv.

The console's MCP client (``[[mcp.servers]]``) can point its ``command`` at any
local stdio server. This backs the Settings → MCP "Install into a venv" one-click:
it puts the server in a venv **outside** the console's own environment — so a heavy
MCP dependency never pollutes the console venv or a fleet install — pip-installs a
named package into it, and hands back the launcher path to wire as the server's
``command``. It is a *generic* installer: it works for any pip-distributed stdio
MCP server, the console never bundles or endorses a specific one.

This module is pure path/normalisation logic (unit-tested on any OS); the bridge
owns the subprocess (venv create + pip install) and the ``[mcp]`` config write.
The venvs live under ``{app_dir}/mcp-venvs/<slug>`` and are machine-local (never
synced — only the resulting ``[[mcp.servers]]`` entry is config).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# venvs land here, one dir per server. Machine-local: the launcher path is
# absolute and per-machine, so a synced config.toml would break on another box —
# the install is re-run there instead.
VENVS_DIRNAME = "mcp-venvs"

# Standard entries a venv's bin/Scripts always has — not the MCP launcher we want.
_STOCK_SCRIPTS = {
    "python",
    "python3",
    "pythonw",
    "pip",
    "pip3",
    "activate",
    "activate.bat",
    "activate.ps1",
    "activate.fish",
    "activate.csh",
    "deactivate.bat",
    "wheel",
    "easy_install",
}


def _is_windows() -> bool:
    return os.name == "nt"


def slugify(name: str) -> str:
    """A safe directory name for a server name (lowercased, non-alnum → ``-``).

    Empty / all-punctuation names collapse to ``server`` so a dir is always made.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "").strip().lower())
    slug = slug.strip("-.") or "server"
    return slug


def venvs_root(app_dir: Path) -> Path:
    return Path(app_dir) / VENVS_DIRNAME


def venv_dir(app_dir: Path, name: str) -> Path:
    """The dedicated venv directory for the MCP server named ``name``."""
    return venvs_root(app_dir) / slugify(name)


def bin_dir(venv: Path) -> Path:
    """The venv's executables dir — ``Scripts`` on Windows, ``bin`` on POSIX."""
    return Path(venv) / ("Scripts" if _is_windows() else "bin")


def venv_python(venv: Path) -> Path:
    """The venv's interpreter path."""
    return bin_dir(venv) / ("python.exe" if _is_windows() else "python")


def launcher_path(venv: Path, command: str) -> Path:
    """The console-script ``command`` inside the venv's bin/Scripts.

    On Windows a pip console script is ``<command>.exe``; the caller passes the
    bare name (as it would appear on ``PATH``) and we add the suffix.
    """
    cmd = str(command or "").strip()
    if _is_windows() and not cmd.lower().endswith(".exe"):
        cmd = f"{cmd}.exe"
    return bin_dir(venv) / cmd


def default_command(package: str) -> str:
    """Best-guess launcher name from a pip spec — the distribution's base name.

    Strips a version specifier (``==1.2`` / ``>=1``), ``[extras]``, and a
    ``name @ url`` direct reference. Most packages' console script matches the
    distribution name; when it doesn't, the operator overrides it. Returns ``""``
    when no plain name can be extracted (e.g. a bare URL/path).
    """
    spec = str(package or "").strip()
    # Drop a PEP 508 direct reference / environment marker / VCS URL tail.
    spec = re.split(r"[@;]", spec, maxsplit=1)[0]
    spec = spec.strip()
    # Take the leading name token, before any extras or version operator.
    m = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", spec)
    return m.group(0) if m else ""


def is_safe_package_spec(package: str) -> bool:
    """Whether ``package`` is a plain named pip spec safe to pass to ``pip install``.

    Guards against argument injection: a single whitespace-free token that does
    not start with ``-`` (so a ``--index-url http://…`` or ``-e path`` can't ride
    in). Allows a name with ``[extras]`` and a version specifier. Rejects URLs,
    local paths, and option-looking values — this installer is for named
    packages from the configured index only.
    """
    spec = str(package or "").strip()
    if not spec or spec.startswith("-") or any(c.isspace() for c in spec):
        return False
    # name[extras]<version-spec> — a conservative allowlist of pip-name chars.
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~.*]*$", spec))


def installed_scripts(venv: Path) -> list[str]:
    """The non-stock console scripts present in the venv's bin/Scripts.

    Used to suggest a ``command`` when the guessed one isn't found after install
    (many packages name their script differently from the distribution). Windows
    ``.exe`` suffixes are stripped so the names are what you'd wire as ``command``.
    """
    d = bin_dir(venv)
    if not d.is_dir():
        return []
    out: list[str] = []
    for entry in sorted(d.iterdir()):
        if not entry.is_file():
            continue
        stem = entry.name
        low = stem.lower()
        # Normalise the Windows launcher suffix for comparison + display.
        if _is_windows() and low.endswith(".exe"):
            stem = stem[:-4]
            low = low[:-4]
        if low in _STOCK_SCRIPTS or stem.startswith("_"):
            continue
        if stem not in out:
            out.append(stem)
    return out

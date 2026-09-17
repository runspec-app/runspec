"""
version.py — the installed runspec-console version and a best-effort update check.

``installed_version()`` reads the version recorded in the package metadata (what
``pip`` actually installed), falling back to the adjacent ``pyproject.toml`` when
the package isn't installed (running straight from a source checkout). There is
no hand-maintained in-tree version constant — ``pyproject.toml`` is the single
source of truth, so the source-run version can never drift out of sync.

``latest_version()`` asks ``pip`` — running from the very interpreter
runspec-console is installed in (``sys.executable``) — what the newest available
release is. Going through that venv's ``pip`` means the check honours its index
configuration: if the venv is pointed at an internal PyPI proxy / mirror, that
is queried instead of public PyPI. It is intentionally best-effort: any failure
(pip missing, offline, index unreachable, locked-down network) returns ``None``
rather than raising, so the "you're up to date / update available" indicator
simply doesn't show instead of breaking the app.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path

from .procutil import no_window_kwargs

PACKAGE_NAME = "runspec-console"

# `pip index versions <pkg>` prints, e.g.:
#     runspec-console (0.19.0)
#     Available versions: 0.19.0, 0.18.0, 0.16.1
#       INSTALLED: 0.18.0
#       LATEST:    0.19.0
# The LATEST/Available lines only appear in some pip versions, so we try each in
# turn and fall back to the version in parentheses on the first line.
_LATEST_RE = re.compile(r"LATEST:\s*(\S+)")
_AVAILABLE_RE = re.compile(r"Available versions:\s*(.+)")
_PAREN_RE = re.compile(r"\(([^)]+)\)\s*$")


def installed_version(package: str = PACKAGE_NAME) -> str:
    """The version pip installed for ``package``.

    For runspec-console itself, falls back to the adjacent ``pyproject.toml`` when
    the package isn't installed (running from a source checkout) — there is no
    hand-maintained version constant to drift out of sync. For a branded wrapper
    package there is no such file to borrow, so an uninstalled one yields ``""``
    and the caller simply skips the update indicator.
    """
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        if package == PACKAGE_NAME:
            return _source_tree_version()
        return ""


def _source_tree_version() -> str:
    """Best-effort version from the ``pyproject.toml`` next to this package.

    Only meaningful in a source checkout: an installed wheel ships no
    ``pyproject.toml``, but then ``metadata.version()`` already succeeds and this
    is never reached. Returns ``""`` if the file is missing or unparseable.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with pyproject.open("rb") as fh:
            version = tomllib.load(fh).get("project", {}).get("version", "")
        return version if isinstance(version, str) else ""
    except (OSError, tomllib.TOMLDecodeError):
        return ""


def latest_version(timeout: float = 8.0, package: str = PACKAGE_NAME) -> str | None:
    """Latest version available from the venv's configured package index.

    Shells out to ``pip`` from ``sys.executable`` (the interpreter
    runspec-console runs in), so an internal index/proxy configured for that
    venv is what gets queried. Never raises — any failure yields ``None`` so the
    caller can quietly skip the update indicator.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "index", "versions", package],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **no_window_kwargs(),
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return _parse_pip_index(proc.stdout, package)


def _parse_pip_index(output: str, package: str = PACKAGE_NAME) -> str | None:
    """Pull the newest version out of ``pip index versions`` stdout."""
    m = _LATEST_RE.search(output)
    if m:
        return m.group(1).strip()
    m = _AVAILABLE_RE.search(output)
    if m:
        first = m.group(1).split(",")[0].strip()
        return first or None
    for line in output.splitlines():
        if line.strip().startswith(package):
            m = _PAREN_RE.search(line)
            if m:
                return m.group(1).strip()
    return None


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse a dotted version into a comparable tuple of leading-int segments.

    Deliberately dependency-free (no ``packaging``): splits on ``.`` and keeps
    each segment's leading digits, so ``"0.19.0"`` → ``(0, 19, 0)`` and a
    pre-release like ``"0.19.0rc1"`` → ``(0, 19, 0)``. Good enough to answer
    "is the published release newer than mine"; a pre-release suffix is ignored
    rather than mis-ranked, which keeps the check conservative.
    """
    parts: list[int] = []
    for segment in v.split("."):
        digits = ""
        for ch in segment:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    """True when ``latest`` is a strictly newer release than ``current``."""
    lt = _version_tuple(latest)
    ct = _version_tuple(current)
    # Pad the shorter tuple so 0.19 and 0.19.0 compare equal.
    width = max(len(lt), len(ct))
    lt += (0,) * (width - len(lt))
    ct += (0,) * (width - len(ct))
    return lt > ct

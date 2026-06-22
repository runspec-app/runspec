"""Command-location runnable: ``which`` — find executables on the search PATH.

This is a pure PATH lookup (no Windows API calls), so unlike the system
runnables it is *not* gated by :func:`_platform.ensure_windows` and behaves
sensibly on any platform. On Windows a bare command name resolves against
``PATHEXT`` (``.EXE``/``.BAT``/…), mirroring how ``where`` / the shell find it.
"""

from __future__ import annotations

import json
import os
import sys

import runspec as rs


def _is_nt() -> bool:
    # Indirection so tests can simulate Windows without flipping the global
    # ``os.name`` (which would make pathlib build WindowsPath off-Windows).
    return os.name == "nt"


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _windows_exts() -> list[str]:
    # PATHEXT is always ';'-delimited on Windows (not os.pathsep — which lets
    # this stay correct, and testable, when simulated off-Windows).
    raw = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    return [e for e in raw.split(";") if e]


def _candidate_files(name: str) -> list[str]:
    """File names to try for ``name``, honouring Windows ``PATHEXT``.

    Off Windows the name is used as-is. On Windows a name that already ends in a
    PATHEXT extension is used as-is; otherwise each extension is appended (so
    ``python`` resolves ``python.exe``).
    """
    if not _is_nt():
        return [name]
    exts = _windows_exts()
    if any(name.lower().endswith(e.lower()) for e in exts):
        return [name]
    return [name + e for e in exts]


def _resolve(name: str, dirs: list[str]) -> list[str]:
    """Return every executable match for ``name`` across ``dirs`` (in order)."""
    cands = _candidate_files(name)
    # A name with a path separator is a path, not a bare command: check it
    # directly rather than searching the PATH.
    if os.sep in name or (os.altsep and os.altsep in name):
        return [os.path.abspath(c) for c in cands if _is_executable(c)]
    seen: set[str] = set()
    matches: list[str] = []
    for d in dirs:
        for c in cands:
            full = os.path.join(d, c)
            key = os.path.normcase(full)  # case-insensitive dedupe on Windows
            if key in seen:
                continue
            seen.add(key)
            if _is_executable(full):
                matches.append(full)
    return matches


def which(
    names: str | list[str],
    *,
    all_matches: bool = False,
    path: str | None = None,
) -> dict:
    """Locate executables on the search PATH, like ``which`` / ``where``.

    ``names`` is a single command name or a list of them; a string is split on
    whitespace, so ``"python pip"`` resolves both. ``all_matches`` returns every
    match across the PATH (not just the first that would run); ``path`` overrides
    the search path (defaults to ``$PATH``, falling back to ``os.defpath``).

    Returns ``{path, all_found, results: [{name, found, path, paths}]}`` and
    raises ``ValueError`` when no name is given.
    """
    if isinstance(names, str):
        names = names.split()
    names = [n for n in names if n]
    if not names:
        raise ValueError("no command name given")

    search = os.environ.get("PATH", os.defpath) if path is None else path
    dirs = [d or os.curdir for d in search.split(os.pathsep)]

    results = []
    for name in names:
        matches = _resolve(name, dirs)
        results.append(
            {
                "name": name,
                "found": bool(matches),
                "path": matches[0] if matches else None,
                "paths": matches if all_matches else matches[:1],
            }
        )
    return {
        "path": search,
        "all_found": all(r["found"] for r in results),
        "results": results,
    }


def main_which() -> None:
    spec = rs.parse("which")
    try:
        result = which(
            spec.name.value,
            all_matches=spec.all.value,
            path=spec.path.value or None,
        )
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    print(json.dumps(result))
    # Mirror the `which`/`where` command: non-zero exit when any name was missing.
    if not result["all_found"]:
        sys.exit(1)

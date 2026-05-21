"""
finder.py — Locates runspec configuration files.

Search strategy:
  find_config(start, caller) — walk up from caller's package directory first
                       (if given), then from start/cwd. The caller-relative
                       pass finds runspec.toml shipped inside an installed
                       package, regardless of where the user shells in from.
                       `runspec jump` passes no caller and walks cwd only.
                       `runspec local` and `runspec serve` use
                       importlib.metadata instead.
"""

from __future__ import annotations

from pathlib import Path


def find_config(start: Path | None = None, *, caller: Path | None = None) -> Path:
    """
    Locate runspec.toml.

    Resolution order:
      1. Walk up from caller's directory, if provided. This is the primary
         strategy for parse() — it lands on the runspec.toml that was bundled
         next to the calling module inside the installed package.
      2. Walk up from `start` (defaults to cwd). Fallback for ad-hoc scripts
         and for tooling that doesn't have a caller (e.g. `runspec jump`).

    Args:
        start:  Directory to start the cwd-walk from. Defaults to cwd.
        caller: File path of the caller (typically the user's module
                that invoked parse()). If given, its parent directory and
                ancestors are searched before the cwd walk.

    Returns:
        Path to the first runspec.toml found.

    Raises:
        FileNotFoundError: if no runspec.toml is found on either path.
    """
    if caller is not None:
        caller_dir = (caller.parent if caller.suffix else caller).resolve()
        for directory in [caller_dir, *caller_dir.parents]:
            candidate = directory / "runspec.toml"
            if candidate.exists():
                return candidate

    search_dir = (start or Path.cwd()).resolve()
    for directory in [search_dir, *search_dir.parents]:
        candidate = directory / "runspec.toml"
        if candidate.exists():
            return candidate

    raise FileNotFoundError("No runspec.toml found.\nRun 'runspec init' to create one, then move it inside your package directory.")

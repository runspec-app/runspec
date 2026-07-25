"""Search helper — the pure logic behind the ``fs`` runnable's ``search-files``
verb. Recursively finds files/directories whose name matches a pattern,
case-insensitively (glob when the pattern contains glob metacharacters, else a
substring match), honouring a list of exclude globs."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

_GLOB_CHARS = set("*?[]")


def _matches(name: str, pattern: str) -> bool:
    lname = name.lower()
    lpat = pattern.lower()
    if _GLOB_CHARS & set(pattern):
        return fnmatch.fnmatch(lname, lpat)
    return lpat in lname


def search_files(
    path: str | os.PathLike[str],
    pattern: str,
    *,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    """Recursively search ``path`` for entries whose name matches ``pattern``.

    ``exclude`` is a list of glob patterns; a directory whose name matches an
    exclude is pruned (not descended into), and matching files are skipped.
    Returns ``{path, pattern, matches: [str], count}``.
    """
    root = Path(path).expanduser()
    exclude = exclude or []
    matches: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in place so os.walk doesn't descend them.
        dirnames[:] = [d for d in dirnames if not any(fnmatch.fnmatch(d, pat) for pat in exclude)]
        for name in list(dirnames) + filenames:
            if any(fnmatch.fnmatch(name, pat) for pat in exclude):
                continue
            if _matches(name, pattern):
                matches.append(str(Path(dirpath) / name))

    matches.sort()
    return {"path": str(root), "pattern": pattern, "matches": matches, "count": len(matches)}

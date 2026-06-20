"""Resolve system tools that may live in sbin dirs not on a non-login PATH.

The console runs runnables over a non-login, non-interactive SSH shell whose
``PATH`` often omits ``/usr/sbin`` and ``/sbin`` — so ``shutil.which()`` can't
find admin tools like ``visudo`` and ``shutdown`` even when they are installed.
``which()`` searches ``PATH`` first, then the standard sbin/bin locations, and
returns an absolute path so the caller can invoke the tool regardless of PATH.
"""

import os
import shutil

# Searched in order after PATH — the standard admin-tool locations.
_FALLBACK_DIRS = (
    "/usr/local/sbin",
    "/usr/sbin",
    "/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
)


def which(name: str) -> str | None:
    """Return the absolute path to ``name`` (PATH first, then sbin/bin), or None."""
    found = shutil.which(name)
    if found:
        return found
    for directory in _FALLBACK_DIRS:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None

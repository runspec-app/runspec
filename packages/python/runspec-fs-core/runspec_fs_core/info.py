"""Metadata helpers — the pure logic behind the ``fs`` runnable's
``get-file-info`` and ``list-allowed-directories`` verbs."""

from __future__ import annotations

import getpass
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def get_file_info(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return detailed metadata for a file or directory: size, timestamps
    (UTC ISO-8601), octal permissions, and type flags. Returns a plain dict."""
    p = Path(path).expanduser()
    st = p.stat()
    return {
        "path": str(p),
        "type": "directory" if p.is_dir() else "file",
        "size": st.st_size,
        "created": _iso(st.st_ctime),
        "modified": _iso(st.st_mtime),
        "accessed": _iso(st.st_atime),
        "permissions": oct(stat.S_IMODE(st.st_mode)),
        "is_directory": p.is_dir(),
        "is_file": p.is_file(),
        "is_symlink": p.is_symlink(),
    }


def allowed_directories() -> dict[str, Any]:
    """Report the effective user and useful starting directories.

    This runnable applies **no software directory jail** — access is bounded by
    the operating-system permissions of the user the process runs as (the
    logged-in user, or a ``run_as`` service account). This verb is informational,
    so an agent knows *who* it is and *where* it starts; it never widens what the
    OS already allows.
    """
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 — getuser can raise if no username resolves
        user = str(os.getuid()) if hasattr(os, "getuid") else "unknown"
    return {
        "user": user,
        "home": str(Path.home()),
        "cwd": os.getcwd(),
        "note": ("No directory jail is enforced. Filesystem access is bounded by the OS permissions of the user this process runs as."),
    }

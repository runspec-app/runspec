"""Directory helpers — the pure logic behind the ``fs`` runnable's directory
verbs (``create-directory``, ``list-directory``, ``list-directory-with-sizes``,
``directory-tree``, ``move-file``).

Each function takes plain params, returns plain data, and raises on failure
(stdlib ``OSError`` family: ``FileNotFoundError``, ``NotADirectoryError``,
``FileExistsError`` …).
"""

from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path
from typing import Any


def _as_path(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser()


def _excluded(name: str, exclude: list[str] | None) -> bool:
    return bool(exclude) and any(fnmatch.fnmatch(name, pat) for pat in exclude)  # type: ignore[union-attr]


def create_directory(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Create a directory (and any missing parents). Idempotent — confirming an
    existing directory succeeds. Returns ``{path, created}`` where ``created`` is
    ``False`` if it already existed."""
    p = _as_path(path)
    existed = p.exists()
    p.mkdir(parents=True, exist_ok=True)
    return {"path": str(p), "created": not existed}


def _entry_type(entry: os.DirEntry[str] | Path) -> str:
    return "directory" if entry.is_dir() else "file"


def list_directory(path: str | os.PathLike[str]) -> dict[str, Any]:
    """List a directory's immediate contents. Returns
    ``{path, entries: [{name, type}]}`` sorted directories-first then by name."""
    p = _as_path(path)
    entries = [{"name": e.name, "type": _entry_type(e)} for e in sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))]
    return {"path": str(p), "entries": entries, "count": len(entries)}


def list_directory_with_sizes(
    path: str | os.PathLike[str],
    *,
    sort_by: str = "name",
) -> dict[str, Any]:
    """List a directory's contents with per-entry sizes and aggregate statistics.

    ``sort_by`` is ``"name"`` (default) or ``"size"`` (descending). Returns
    ``{path, entries: [{name, type, size}], total_files, total_directories,
    total_size}``.
    """
    if sort_by not in ("name", "size"):
        raise ValueError("sort_by must be 'name' or 'size'")
    p = _as_path(path)

    entries: list[dict[str, Any]] = []
    total_files = 0
    total_dirs = 0
    total_size = 0
    for e in p.iterdir():
        is_dir = e.is_dir()
        size = 0 if is_dir else e.stat().st_size
        entries.append({"name": e.name, "type": "directory" if is_dir else "file", "size": size})
        if is_dir:
            total_dirs += 1
        else:
            total_files += 1
            total_size += size

    if sort_by == "size":
        entries.sort(key=lambda x: x["size"], reverse=True)
    else:
        entries.sort(key=lambda x: (x["type"] == "file", x["name"].lower()))

    return {
        "path": str(p),
        "entries": entries,
        "total_files": total_files,
        "total_directories": total_dirs,
        "total_size": total_size,
    }


def directory_tree(
    path: str | os.PathLike[str],
    *,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    """Return a recursive JSON tree of a directory. Each node is
    ``{name, type}`` and directories carry a ``children`` list. ``exclude`` is a
    list of glob patterns matched against entry names at every level."""
    root = _as_path(path)

    def build(node: Path) -> dict[str, Any]:
        result: dict[str, Any] = {"name": node.name, "type": "directory"}
        children: list[dict[str, Any]] = []
        for child in sorted(node.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
            if _excluded(child.name, exclude):
                continue
            if child.is_dir():
                children.append(build(child))
            else:
                children.append({"name": child.name, "type": "file"})
        result["children"] = children
        return result

    tree = build(root)
    # The root node's name is the full path so the caller knows what was walked.
    tree["name"] = str(root)
    return {"path": str(root), "tree": tree}


def move_file(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> dict[str, Any]:
    """Move or rename a file or directory. Fails if the destination already
    exists (mirrors the MCP tool — no silent overwrite). Returns
    ``{source, destination}``."""
    src = _as_path(source)
    dst = _as_path(destination)
    if dst.exists():
        raise FileExistsError(f"destination already exists: {dst}")
    shutil.move(str(src), str(dst))
    return {"source": str(src), "destination": str(dst)}

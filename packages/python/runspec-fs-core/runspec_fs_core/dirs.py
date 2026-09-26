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

from runspec_fs_core._mounts import MountGuard


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


#: How many unreadable directory paths ``directory_size`` names in its result.
UNREADABLE_SAMPLE = 10


class _ScanErrors:
    """Tally of entries a size scan couldn't read, plus the first few
    directories it couldn't open (so the caller can say *where* totals are short)."""

    def __init__(self) -> None:
        self.count = 0
        self.unreadable_dirs: list[str] = []

    def dir_denied(self, path: Path) -> None:
        self.count += 1
        if len(self.unreadable_dirs) < UNREADABLE_SAMPLE:
            self.unreadable_dirs.append(str(path))


def _running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def _measure_dir(
    root: Path,
    depth: int,
    limit: int,
    errors: _ScanErrors,
    guard: MountGuard,
    dev: int,
) -> tuple[int, list[dict[str, Any]] | None, int]:
    """Recursively measure ``root``, which is on device ``dev``. Returns
    ``(total, entries, child_count)``.

    ``total`` is the sum of the sizes of every regular file beneath ``root``
    (recursive, symlinks never followed so a link target is never double-counted
    or looped). When ``depth >= 1`` the children are also broken out: ``entries``
    is this directory's children sorted largest-first and capped at ``limit``
    (``limit <= 0`` = no cap), and ``child_count`` is the pre-cap number of
    children; a subdirectory carries its own breakdown under ``children`` while
    ``depth`` remains. When ``depth < 1`` only the total is needed, so ``entries``
    is ``None`` (the full subtree is still walked to sum the total). Unreadable
    entries (permissions, races) are skipped and tallied in ``errors`` rather
    than aborting — essential when scanning system directories as a non-root user.
    A directory that can't be opened at all returns a child count of ``-1`` so
    the caller can flag its record ``unreadable`` (its size is a floor of 0).
    A subdirectory the ``guard`` won't enter (another filesystem, or a virtual
    one) counts as 0 and is listed with ``skipped_mount: true``."""
    total = 0
    entries: list[dict[str, Any]] = []
    try:
        scan = os.scandir(root)
    except OSError:
        errors.dir_denied(root)
        return 0, ([] if depth >= 1 else None), -1
    with scan:
        for entry in scan:
            skipped_mount = False
            unreadable = False
            try:
                if entry.is_symlink():
                    etype = "symlink"
                    size = entry.stat(follow_symlinks=False).st_size
                    child_entries = None
                elif entry.is_dir(follow_symlinks=False):
                    etype = "directory"
                    # os.stat, not entry.stat(): a Windows DirEntry's st_dev is
                    # always 0, and following resolves a folder-mounted volume to
                    # the volume behind it (a non-symlink POSIX dir stats the same).
                    child_dev = os.stat(entry.path).st_dev
                    if guard.enter(entry.path, child_dev, dev):
                        size, child_entries, child_n = _measure_dir(Path(entry.path), depth - 1, limit, errors, guard, child_dev)
                        unreadable = child_n < 0
                    else:
                        size, child_entries, skipped_mount = 0, None, True
                else:
                    etype = "file"
                    size = entry.stat(follow_symlinks=False).st_size
                    child_entries = None
            except OSError:
                errors.count += 1
                continue
            total += size
            if depth >= 1:
                rec: dict[str, Any] = {
                    "name": entry.name,
                    "path": entry.path,
                    "type": etype,
                    "size": size,
                    "size_mb": round(size / 1_048_576, 2),
                }
                if child_entries:  # a subdirectory's breakdown, while depth remains
                    rec["children"] = child_entries
                if skipped_mount:
                    rec["skipped_mount"] = True
                if unreadable:
                    rec["unreadable"] = True
                entries.append(rec)

    if depth < 1:
        return total, None, 0
    entries.sort(key=lambda e: e["size"], reverse=True)
    capped = entries[:limit] if limit and limit > 0 else entries
    return total, capped, len(entries)


def directory_size(
    path: str | os.PathLike[str],
    *,
    limit: int = 20,
    depth: int = 1,
    cross_mounts: bool = False,
) -> dict[str, Any]:
    """Report the recursive total size of each child of a directory, largest
    first — the "what's using the disk here" view (like ``du -x -d<depth> | sort``).

    Unlike ``list_directory_with_sizes`` (which reports 0 for a directory entry),
    each subdirectory's ``size`` is the total of every file beneath it, so an
    agent investigating a full drive can see which folder is the culprit and
    drill down. ``depth`` (default 1, immediate children only; clamped to a
    minimum of 1) nests the breakdown that many levels deep in one call — a
    directory entry carries its own sorted children under ``children`` while
    depth remains, so several levels can be seen without a call per level.
    Symlinks are not followed. ``total_size`` is the true total of everything
    scanned even when ``entries`` is truncated to the top ``limit`` (which caps
    each level; ``limit <= 0`` returns all). Unreadable entries (permissions,
    races) are skipped and counted under ``skipped`` rather than failing the scan;
    a directory that couldn't be opened is listed with ``unreadable: true`` (its
    size a floor of 0), the first few such paths are named in ``unreadable``, and
    a ``hint`` says totals are incomplete and to re-run as root — so a scan of
    ``/`` as a normal user can't silently under-report (e.g. Docker volumes under
    ``/var/lib/docker`` counted as 0).

    The scan stays on the filesystem ``path`` is on (like ``du -x``) unless
    ``cross_mounts`` is set; kernel virtual filesystems (``/proc``, ``/sys``,
    ``/dev`` …) are never scanned either way, so a scan of ``/`` can't count
    ``/proc/kcore`` (~128 TiB). A subdirectory not entered for either reason
    counts as 0, is listed with ``skipped_mount: true``, and is tallied in
    ``skipped_mounts``. Raises ``ValueError`` when ``path`` itself is on a
    virtual filesystem.

    Returns ``{path, total_size, total_mb, entry_count, entries:
    [{name, path, type, size, size_mb, children?, skipped_mount?, unreadable?}],
    skipped?, unreadable?, hint?, skipped_mounts?}``.
    """
    p = _as_path(path)
    if not p.is_dir():
        raise NotADirectoryError(f"not a directory: {p}")
    guard = MountGuard(p, cross_mounts=cross_mounts)

    errors = _ScanErrors()
    total, entries, child_count = _measure_dir(p, max(1, depth), limit, errors, guard, os.stat(p).st_dev)
    result: dict[str, Any] = {
        "path": str(p),
        "total_size": total,
        "total_mb": round(total / 1_048_576, 2),
        "entry_count": child_count,
        "entries": entries or [],
    }
    if errors.count:
        result["skipped"] = errors.count
        if errors.unreadable_dirs:
            result["unreadable"] = errors.unreadable_dirs
        if not _running_as_root():
            how = "as root (the runnable's --run-as root, or sudo)" if hasattr(os, "geteuid") else "elevated (as Administrator)"
            result["hint"] = (
                f"{errors.count} entr{'y' if errors.count == 1 else 'ies'} could not be read (permission denied), so these totals "
                f"are a lower bound — sizes under unreadable directories count as 0. Re-run {how} for complete totals."
            )
    if guard.skipped_mounts:
        result["skipped_mounts"] = guard.skipped_mounts
    return result


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

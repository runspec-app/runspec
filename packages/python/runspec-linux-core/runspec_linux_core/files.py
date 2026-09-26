"""File helpers: find large files, create a timestamped tar.gz backup."""

import os
import stat
import tarfile
from datetime import datetime
from pathlib import Path

from runspec_linux_core._mounts import MountGuard


def find_large_files_report(search_path: str, min_mb: float = 100.0, limit: int = 50, *, cross_mounts: bool = False) -> dict:
    """Find files under ``search_path`` at least ``min_mb`` MB, biggest first.

    The scan stays on the filesystem ``search_path`` is on, like
    ``find -xdev``, unless ``cross_mounts`` is set. Kernel virtual filesystems
    (``/proc``, ``/sys``, ``/dev`` …) are never scanned either way, so a scan of
    ``/`` can't report ``/proc/kcore`` as a ~128 TiB file. Directories not
    entered for either reason are counted in ``skipped_mounts`` (present only
    when non-zero).

    Returns ``{path, min_mb, files: [{path, size_mb, modified}], match_count,
    skipped_mounts?}``, where ``match_count`` is the number of matching files
    before ``limit`` is applied. Raises ``FileNotFoundError`` /
    ``NotADirectoryError`` for a bad ``search_path`` and ``ValueError`` when it
    is on a virtual filesystem.
    """
    if not os.path.isdir(search_path):
        if not os.path.exists(search_path):
            raise FileNotFoundError(f"no such directory: {search_path}")
        raise NotADirectoryError(f"not a directory: {search_path}")
    guard = MountGuard(search_path, cross_mounts=cross_mounts)
    min_bytes = int(min_mb * 1_048_576)

    results: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(search_path):
        try:
            dir_dev = os.stat(dirpath).st_dev
        except OSError:
            dirnames[:] = []
            continue
        admitted = []
        for name in dirnames:
            sub = os.path.join(dirpath, name)
            try:
                sub_stat = os.lstat(sub)
            except OSError:
                continue
            if stat.S_ISLNK(sub_stat.st_mode):
                continue  # os.walk never follows a symlinked directory anyway
            if guard.enter(sub, sub_stat.st_dev, dir_dev):
                admitted.append(name)
        dirnames[:] = admitted  # prune in place: os.walk descends only into these

        for name in filenames:
            fpath = os.path.join(dirpath, name)
            try:
                file_stat = os.stat(fpath, follow_symlinks=False)
                if file_stat.st_size >= min_bytes:
                    results.append(
                        {
                            "path": fpath,
                            "size_mb": round(file_stat.st_size / 1_048_576, 2),
                            "modified": datetime.fromtimestamp(file_stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
            except OSError:
                continue

    results.sort(key=lambda r: r["size_mb"], reverse=True)
    report: dict = {"path": search_path, "min_mb": min_mb, "files": results[:limit], "match_count": len(results)}
    if guard.skipped_mounts:
        report["skipped_mounts"] = guard.skipped_mounts
    return report


def find_large_files(search_path: str, min_mb: float = 100.0, limit: int = 50, *, cross_mounts: bool = False) -> list[dict]:
    """Return files under ``search_path`` at least ``min_mb`` MB, biggest first.

    The ``files`` list of :func:`find_large_files_report`, with the same
    filesystem boundary (one filesystem unless ``cross_mounts``; virtual
    filesystems never) and the same errors for a bad ``search_path``.
    """
    return find_large_files_report(search_path, min_mb, limit, cross_mounts=cross_mounts)["files"]


def backup_files(source: str, destination: str) -> dict:
    """Create a timestamped ``.tar.gz`` of ``source`` inside ``destination``.

    Returns the archive path and its size.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    source_name = Path(source).name or "backup"
    archive_name = f"{source_name}_{timestamp}.tar.gz"
    archive_path = os.path.join(destination, archive_name)

    os.makedirs(destination, exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(source, arcname=source_name)

    size_bytes = os.path.getsize(archive_path)
    return {
        "source": source,
        "destination": archive_path,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / 1_048_576, 2),
    }

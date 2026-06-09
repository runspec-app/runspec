"""File helpers: find large files, create a timestamped tar.gz backup."""

import os
import tarfile
from datetime import datetime
from pathlib import Path


def find_large_files(search_path: str, min_mb: float = 100.0, limit: int = 50) -> list[dict]:
    """Return files under ``search_path`` at least ``min_mb`` MB, biggest first."""
    min_bytes = int(min_mb * 1_048_576)

    results: list[dict] = []
    for dirpath, _dirs, filenames in os.walk(search_path):
        for name in filenames:
            fpath = os.path.join(dirpath, name)
            try:
                stat = os.stat(fpath, follow_symlinks=False)
                if stat.st_size >= min_bytes:
                    results.append(
                        {
                            "path": fpath,
                            "size_mb": round(stat.st_size / 1_048_576, 2),
                            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
            except OSError:
                continue

    results.sort(key=lambda r: r["size_mb"], reverse=True)
    return results[:limit]


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

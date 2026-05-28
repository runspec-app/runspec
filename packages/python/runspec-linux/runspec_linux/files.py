import json
import os
import sys
import tarfile
from datetime import datetime
from pathlib import Path

import runspec as rs


def main_find_large_files() -> None:
    spec = rs.parse("find-large-files")
    search_path = str(spec.path)
    min_mb = float(spec.min_mb)
    limit = int(spec.limit)

    min_bytes = int(min_mb * 1_048_576)

    try:
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
        print(json.dumps(results[:limit]))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_backup_files() -> None:
    spec = rs.parse("backup-files")
    source = str(spec.source)
    destination = str(spec.destination)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    source_name = Path(source).name or "backup"
    archive_name = f"{source_name}_{timestamp}.tar.gz"
    archive_path = os.path.join(destination, archive_name)

    try:
        os.makedirs(destination, exist_ok=True)
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(source, arcname=source_name)

        size_bytes = os.path.getsize(archive_path)
        print(
            json.dumps(
                {
                    "source": source,
                    "destination": archive_path,
                    "size_bytes": size_bytes,
                    "size_mb": round(size_bytes / 1_048_576, 2),
                }
            )
        )
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

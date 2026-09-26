import json
import sys

import runspec as rs
from runspec_linux_core import backup_files, find_large_files_report


def main_find_large_files() -> None:
    spec = rs.parse("find-large-files")
    search_path = str(spec.path.value)
    min_mb = spec.min_mb.value
    limit = spec.limit.value
    cross_mounts = spec.cross_mounts.value
    try:
        print(json.dumps(find_large_files_report(search_path, min_mb, limit, cross_mounts=cross_mounts)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_backup_files() -> None:
    spec = rs.parse("backup-files")
    source = str(spec.source.value)
    destination = str(spec.destination.value)
    try:
        print(json.dumps(backup_files(source, destination)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

import json
import sys

import runspec as rs
from runspec_linux_core import backup_files, find_large_files


def main_find_large_files() -> None:
    spec = rs.parse("find-large-files")
    search_path = str(spec.path)
    min_mb = float(spec.min_mb)
    limit = int(spec.limit)
    try:
        print(json.dumps(find_large_files(search_path, min_mb, limit)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_backup_files() -> None:
    spec = rs.parse("backup-files")
    source = str(spec.source)
    destination = str(spec.destination)
    try:
        print(json.dumps(backup_files(source, destination)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

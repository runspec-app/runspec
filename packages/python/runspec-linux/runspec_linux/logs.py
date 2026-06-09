import json
import re
import subprocess
import sys

import runspec as rs
from runspec_linux_core import journalctl, search_log, tail_log
from runspec_linux_core.errors import ToolNotFoundError


def main_tail_log() -> None:
    spec = rs.parse("tail-log")
    file_path = str(spec.file)
    lines = int(spec.lines)
    try:
        print(json.dumps(tail_log(file_path, lines)))
    except OSError as e:
        print(json.dumps({"error": str(e), "file": file_path}))
        sys.exit(1)


def main_search_log() -> None:
    spec = rs.parse("search-log")
    file_path = str(spec.file)
    pattern = str(spec.pattern)
    limit = int(spec.limit)
    try:
        print(json.dumps(search_log(file_path, pattern, limit)))
    except OSError as e:
        print(json.dumps({"error": str(e), "file": file_path}))
        sys.exit(1)
    except re.error as e:
        print(json.dumps({"error": f"Invalid regex: {e}", "pattern": pattern}))
        sys.exit(1)


def main_journalctl() -> None:
    spec = rs.parse("journalctl")
    unit = str(spec.unit)
    lines = int(spec.lines)
    since = str(spec.since) if spec.since is not None else None
    try:
        print(json.dumps(journalctl(unit, lines, since)))
    except ToolNotFoundError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(json.dumps({"error": e.stderr.strip(), "unit": unit}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

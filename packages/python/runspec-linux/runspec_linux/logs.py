import json
import re
import subprocess
import sys

import runspec as rs
from runspec_linux_core import journalctl, search_log, tail_log
from runspec_linux_core.errors import CommandError, ToolNotFoundError

from runspec_linux._escalation import resolve_escalation


def main_tail_log() -> None:
    spec = rs.parse("tail-log")
    file_path = str(spec.file.value)
    lines = spec.lines.value
    run_as, method = resolve_escalation(spec)
    try:
        print(json.dumps(tail_log(file_path, lines, run_as=run_as, become_method=method)))
    except CommandError as e:
        print(json.dumps({"error": str(e), "file": file_path}))
        sys.exit(1)
    except OSError as e:
        print(json.dumps({"error": str(e), "file": file_path}))
        sys.exit(1)


def main_search_log() -> None:
    spec = rs.parse("search-log")
    file_path = str(spec.file.value)
    pattern = spec.pattern.value
    limit = spec.limit.value
    run_as, method = resolve_escalation(spec)
    try:
        print(json.dumps(search_log(file_path, pattern, limit, run_as=run_as, become_method=method)))
    except CommandError as e:
        print(json.dumps({"error": str(e), "file": file_path}))
        sys.exit(1)
    except OSError as e:
        print(json.dumps({"error": str(e), "file": file_path}))
        sys.exit(1)
    except re.error as e:
        print(json.dumps({"error": f"Invalid regex: {e}", "pattern": pattern}))
        sys.exit(1)


def main_journalctl() -> None:
    spec = rs.parse("journalctl")
    unit = spec.unit.value
    lines = spec.lines.value
    since = spec.since.value
    run_as, method = resolve_escalation(spec)
    try:
        print(json.dumps(journalctl(unit, lines, since, run_as=run_as, become_method=method)))
    except ToolNotFoundError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(json.dumps({"error": e.stderr.strip(), "unit": unit}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

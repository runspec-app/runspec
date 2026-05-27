import json
import re
import shutil
import subprocess
import sys

import runspec as rs


def main_tail_log() -> None:
    spec = rs.parse("tail-log")
    file_path = str(spec.file)
    lines = int(spec.lines)

    try:
        with open(file_path, errors="replace") as f:
            all_lines = f.readlines()
        tail = [line.rstrip("\n") for line in all_lines[-lines:]]
        print(json.dumps({"file": file_path, "lines": tail, "count": len(tail)}))
    except OSError as e:
        print(json.dumps({"error": str(e), "file": file_path}))
        sys.exit(1)


def main_search_log() -> None:
    spec = rs.parse("search-log")
    file_path = str(spec.file)
    pattern = str(spec.pattern)
    limit = int(spec.limit)

    try:
        regex = re.compile(pattern, re.IGNORECASE)
        matches: list[str] = []
        with open(file_path, errors="replace") as f:
            for line in f:
                if regex.search(line):
                    matches.append(line.rstrip("\n"))
        trimmed = matches[-limit:] if len(matches) > limit else matches
        print(
            json.dumps(
                {
                    "file": file_path,
                    "pattern": pattern,
                    "matches": trimmed,
                    "count": len(trimmed),
                    "total_matches": len(matches),
                }
            )
        )
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

    if not shutil.which("journalctl"):
        print(json.dumps({"error": "journalctl not available — not a systemd system"}))
        sys.exit(1)

    try:
        cmd = ["journalctl", f"-u{unit}", f"-n{lines}", "--no-pager", "--output=short"]
        if since:
            cmd += [f"--since={since}"]

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        output_lines = result.stdout.strip().splitlines()
        print(json.dumps({"unit": unit, "lines": output_lines, "count": len(output_lines)}))
    except subprocess.CalledProcessError as e:
        print(json.dumps({"error": e.stderr.strip(), "unit": unit}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

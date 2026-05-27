import json
import shutil
import subprocess
import sys

import runspec as rs


def main_last_logins() -> None:
    spec = rs.parse("last-logins")
    limit = int(spec.limit)

    if not shutil.which("last"):
        print(json.dumps({"error": "last command not found"}))
        sys.exit(1)

    try:
        result = subprocess.run(
            ["last", "-n", str(limit), "--time-format", "iso"],
            capture_output=True,
            text=True,
        )
        rows = []
        for line in result.stdout.strip().splitlines():
            if not line or line.startswith("wtmp") or line.startswith("btmp"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            rows.append(
                {
                    "user": parts[0],
                    "terminal": parts[1],
                    "from": parts[2] if len(parts) > 2 else "",
                    "login_time": parts[3] if len(parts) > 3 else "",
                    "logout_time": parts[5] if len(parts) > 5 else "",
                }
            )
        print(json.dumps(rows[:limit]))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_who() -> None:
    rs.parse("who")

    if not shutil.which("who"):
        print(json.dumps({"error": "who command not found"}))
        sys.exit(1)

    try:
        result = subprocess.run(["who"], capture_output=True, text=True, check=True)
        rows = []
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            rows.append(
                {
                    "user": parts[0],
                    "terminal": parts[1],
                    "login_time": " ".join(parts[2:4]) if len(parts) > 3 else parts[2],
                    "from": parts[4].strip("()") if len(parts) > 4 else "",
                }
            )
        print(json.dumps(rows))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

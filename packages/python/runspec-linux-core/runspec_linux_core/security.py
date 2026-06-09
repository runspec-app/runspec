"""Security helpers: recent logins, currently logged-in users."""

import shutil
import subprocess

from runspec_linux_core.errors import ToolNotFoundError


def last_logins(limit: int = 20) -> list[dict]:
    """Return the most recent logins via ``last``.

    Raises ToolNotFoundError if ``last`` is not on PATH.
    """
    if not shutil.which("last"):
        raise ToolNotFoundError("last command not found")

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
    return rows[:limit]


def who() -> list[dict]:
    """Return currently logged-in users via ``who``.

    Raises ToolNotFoundError if ``who`` is not on PATH.
    """
    if not shutil.which("who"):
        raise ToolNotFoundError("who command not found")

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
    return rows

"""_platform.py — Windows execution helpers shared by the system runnables.

The OS-specific work (shelling out to ``powershell``/``sc``/``netstat`` etc.)
lives behind these thin helpers so the pure parsers in each module stay
import-clean and unit-testable on any platform. Every Windows-only runnable
calls :func:`ensure_windows` first, so running one on a non-Windows host returns
a structured JSON error instead of a traceback.
"""

from __future__ import annotations

import json
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"


def ensure_windows() -> None:
    """Emit a JSON error and exit if not running on Windows.

    Keeps non-Windows behaviour predictable (clean error, exit 1) rather than
    crashing on a missing ``ipconfig``/``sc``/Win32 API.
    """
    if not IS_WINDOWS:
        print(json.dumps({"error": "This runnable requires Windows", "platform": sys.platform}))
        sys.exit(1)


def fail(message: str, **extra: object) -> None:
    """Print ``{"error": message, **extra}`` as JSON and exit non-zero."""
    print(json.dumps({"error": message, **extra}))
    sys.exit(1)


def run_powershell(script: str, timeout: int = 30) -> str:
    """Run a PowerShell snippet non-interactively and return its stdout.

    Raises ``RuntimeError`` with stderr on a non-zero exit so callers can turn
    it into a JSON error.
    """
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"powershell exited {result.returncode}")
    return result.stdout


def run_cmd(args: list[str], timeout: int = 30, check: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a console command and return the completed process (text mode)."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        encoding="utf-8",
        errors="replace",
    )

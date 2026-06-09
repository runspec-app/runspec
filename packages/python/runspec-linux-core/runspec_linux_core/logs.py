"""Log helpers: tail a file, search a file, read journald units."""

import re
import shutil
import subprocess

from runspec_linux_core.errors import ToolNotFoundError


def tail_log(file_path: str, lines: int = 50) -> dict:
    """Return the last ``lines`` lines of a text file.

    Propagates OSError if the file can't be read.
    """
    with open(file_path, errors="replace") as f:
        all_lines = f.readlines()
    tail = [line.rstrip("\n") for line in all_lines[-lines:]]
    return {"file": file_path, "lines": tail, "count": len(tail)}


def search_log(file_path: str, pattern: str, limit: int = 100) -> dict:
    """Return up to ``limit`` (most recent) lines of a file matching ``pattern``.

    Propagates OSError if the file can't be read, or re.error on a bad pattern.
    """
    regex = re.compile(pattern, re.IGNORECASE)
    matches: list[str] = []
    with open(file_path, errors="replace") as f:
        for line in f:
            if regex.search(line):
                matches.append(line.rstrip("\n"))
    trimmed = matches[-limit:] if len(matches) > limit else matches
    return {
        "file": file_path,
        "pattern": pattern,
        "matches": trimmed,
        "count": len(trimmed),
        "total_matches": len(matches),
    }


def journalctl(unit: str, lines: int = 50, since: str | None = None) -> dict:
    """Return recent journald log lines for a systemd unit.

    Raises ToolNotFoundError if journalctl is not present; propagates
    subprocess.CalledProcessError if journalctl exits non-zero.
    """
    if not shutil.which("journalctl"):
        raise ToolNotFoundError("journalctl not available — not a systemd system")

    cmd = ["journalctl", f"-u{unit}", f"-n{lines}", "--no-pager", "--output=short"]
    if since:
        cmd += [f"--since={since}"]

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    output_lines = result.stdout.strip().splitlines()
    return {"unit": unit, "lines": output_lines, "count": len(output_lines)}

"""Log helpers: tail a file, search a file, read journald units.

Corporate application logs are often root-owned (or owned by a service
account), so each helper takes an optional ``run_as`` user (with a
``become_method``). When set, the read is performed by a subprocess escalated
via ``sudo -n -u`` / ``su`` (:func:`runspec_linux_core.become.build_become`) — a
faithful escalation of the same read, bounded by that user's OS permissions.
With no ``run_as`` the read stays in-process (unchanged).
"""

import re
import shutil
import subprocess

from runspec_linux_core.become import build_become, check_become_error
from runspec_linux_core.errors import ToolNotFoundError


def tail_log(file_path: str, lines: int = 50, *, run_as: str | None = None, become_method: str = "sudo") -> dict:
    """Return the last ``lines`` lines of a text file.

    With ``run_as`` set the read is escalated via ``tail -n`` under sudo/su (so a
    root-owned log is readable); otherwise it reads in-process. Propagates OSError
    if an in-process read fails, or CommandError if the escalated read fails.
    """
    if run_as:
        argv = build_become(["tail", "-n", str(lines), file_path], run_as, become_method)
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            check_become_error("tail log", result.stderr, run_as)
        tail = result.stdout.splitlines()
        return {"file": file_path, "lines": tail, "count": len(tail), "run_as": run_as}

    with open(file_path, errors="replace") as f:
        all_lines = f.readlines()
    tail = [line.rstrip("\n") for line in all_lines[-lines:]]
    return {"file": file_path, "lines": tail, "count": len(tail)}


def search_log(file_path: str, pattern: str, limit: int = 100, *, run_as: str | None = None, become_method: str = "sudo") -> dict:
    """Return up to ``limit`` (most recent) lines of a file matching ``pattern``.

    The match is always Python ``re`` (case-insensitive). With ``run_as`` set the
    file is read via ``cat`` under sudo/su (so a root-owned log is searchable) and
    the same regex is applied; otherwise it streams the file in-process. Propagates
    OSError / re.error in-process, or CommandError if the escalated read fails.
    """
    regex = re.compile(pattern, re.IGNORECASE)
    matches: list[str] = []

    if run_as:
        argv = build_become(["cat", file_path], run_as, become_method)
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            check_become_error("read log", result.stderr, run_as)
        for line in result.stdout.splitlines():
            if regex.search(line):
                matches.append(line)
    else:
        with open(file_path, errors="replace") as f:
            for line in f:
                if regex.search(line):
                    matches.append(line.rstrip("\n"))

    trimmed = matches[-limit:] if len(matches) > limit else matches
    result_dict = {
        "file": file_path,
        "pattern": pattern,
        "matches": trimmed,
        "count": len(trimmed),
        "total_matches": len(matches),
    }
    if run_as:
        result_dict["run_as"] = run_as
    return result_dict


def journalctl(unit: str, lines: int = 50, since: str | None = None, *, run_as: str | None = None, become_method: str = "sudo") -> dict:
    """Return recent journald log lines for a systemd unit.

    With ``run_as`` set the query is escalated via sudo/su (so protected units
    outside the ``systemd-journal`` group are readable). Raises ToolNotFoundError
    if journalctl is not present, or CommandError if the query exits non-zero.
    """
    if not shutil.which("journalctl"):
        raise ToolNotFoundError("journalctl not available — not a systemd system")

    cmd = ["journalctl", f"-u{unit}", f"-n{lines}", "--no-pager", "--output=short"]
    if since:
        cmd += [f"--since={since}"]

    result = subprocess.run(build_become(cmd, run_as, become_method), capture_output=True, text=True)
    if result.returncode != 0:
        check_become_error("journalctl", result.stderr, run_as)
    output_lines = result.stdout.strip().splitlines()
    out = {"unit": unit, "lines": output_lines, "count": len(output_lines)}
    if run_as:
        out["run_as"] = run_as
    return out

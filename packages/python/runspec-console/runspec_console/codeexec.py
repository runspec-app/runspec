"""
codeexec.py — the pure primitives behind the ``run_python`` / ``run_sql`` agent
tools (see ``bridge_agent_tools._run_exec_tool``).

Kept out of ``workspaces.py`` on purpose: that module's contract is
"pure filesystem helpers, no subprocess". These helpers DO shell out (Python) or
open a database (SQLite), rooted in — and, for file arguments, jailed to — the
current session's workspace via :func:`runspec_console.workspaces.jail`.

Trust posture (deliberate)
==========================
The console is single-user and runs **as the operator**. ``run_python``
executes arbitrary code with the operator's full privileges — the path-jail only
bounds *which file the tool is pointed at* and the subprocess cwd; it does **not**
sandbox what the code can touch. So **the confirm gate (in the autonomy layer) is
the security control here, not the jail.** We add a wall-clock timeout and output
caps/spill because those are honest, proportionate safety rails; we do **not**
attempt OS sandboxing / network isolation / hard rlimits, which would be security
theater on a Windows-first desktop console whose operator already has
shell-equivalent power over the whole fleet via SSH runnables.

Every function is pure (no ``self``, no Bridge state), so it is unit-testable on
any OS.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .bridge_const import ToolCancelled
from .workspaces import jail

# How often the run_python poll loop wakes to check the timeout / cancel flag.
_POLL_INTERVAL_S = 0.05

# SQL leading keywords that only read — everything else (INSERT/UPDATE/DELETE/
# CREATE/DROP/ALTER/REPLACE/PRAGMA-write/…) is treated as a write and thus
# confirm-gated. WITH … <SELECT> is a read; a writable CTE (WITH … DELETE) is not.
_SQL_READ_LEADERS = frozenset({"select", "explain", "with", "values"})


def classify_sql(query: str) -> str:
    """``"read"`` for a query that only reads (``SELECT``/``EXPLAIN``/read
    ``PRAGMA``/``VALUES``/``WITH … SELECT``), else ``"write"``.

    Conservative: anything it can't confidently call a read is a write, so the
    autonomy gate errs toward prompting. Used by ``_effective_tool_autonomy`` to
    let read-only ``run_sql`` run autonomously while writes/DDL confirm-gate."""
    q = (query or "").strip().lstrip("(").strip()
    if not q:
        return "write"
    first = q.split(None, 1)[0].lower()
    if first == "pragma":
        # A bare `PRAGMA foo` (or `PRAGMA foo(x)`) reads; `PRAGMA foo = bar` writes.
        return "write" if "=" in q else "read"
    if first not in _SQL_READ_LEADERS:
        return "write"
    if first == "with":
        # A CTE is only a read when the outer statement is a SELECT; a data-
        # modifying CTE (WITH … DELETE/UPDATE/INSERT) writes.
        low = q.lower()
        if any(kw in low for kw in (" delete ", " update ", " insert ")):
            return "write"
    return "read"


def run_sql(
    db_path: Path,
    query: str,
    *,
    read_only: bool,
    max_rows: int,
) -> dict[str, Any]:
    """Run ``query`` against the sqlite database at ``db_path`` (already jailed to
    the workspace by the caller) and return a bounded result dict.

    ``read_only`` opens the database via a ``mode=ro`` URI so a query classified
    as a read *cannot* mutate even if the classifier was fooled — the autonomous
    path is enforced at the connection, not just by inspection. Rows past
    ``max_rows`` are dropped and ``truncated`` is set. Runs in-process with stdlib
    ``sqlite3`` (no code execution)."""
    if read_only:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(query)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows: list[list[Any]] = []
        truncated = False
        if columns:
            for row in cur:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append(list(row))
        if not read_only:
            conn.commit()
        return {
            "columns": columns,
            "rows": rows,
            "rowcount": cur.rowcount,
            "returned": len(rows),
            "truncated": truncated,
        }
    finally:
        conn.close()


def run_python(
    root: Path,
    *,
    code: str | None = None,
    file_rel: str | None = None,
    timeout_s: int,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run a Python snippet (``code``, via stdin) or a workspace file
    (``file_rel``, jailed under ``root``) with the console's own interpreter,
    ``cwd`` set to the workspace.

    Honours a cooperative ``cancel`` predicate (the operator's Stop / the tool
    card's Cancel) and a wall-clock ``timeout_s``; either kills the process. A
    set ``cancel`` raises :class:`ToolCancelled` so the tracked-tool wrapper turns
    it into a clean "⏹ stopped" result. Returns
    ``{stdout, stderr, exit_code, timed_out}``."""
    if bool(code) == bool(file_rel):
        raise ValueError("run_python needs exactly one of `code` or `file`")

    if file_rel:
        target = jail(root, file_rel)  # raises ValueError on escape
        if not target.is_file():
            raise FileNotFoundError(f"no such workspace file: {file_rel}")
        argv = [sys.executable, str(target)]
        stdin_data: str | None = None
    else:
        argv = [sys.executable, "-"]
        stdin_data = code

    proc = subprocess.Popen(
        argv,
        cwd=str(root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Drain stdout/stderr on background threads: reading in the poll loop would
    # deadlock a child that fills a pipe buffer, and communicate() can't be
    # combined with a cooperative cancel/timeout poll. Each thread blocks on
    # read() until EOF (the child exits or is killed and its pipes close).
    out_box: list[str] = []
    err_box: list[str] = []

    def _drain(stream: Any, box: list[str]) -> None:
        try:
            box.append(stream.read())
        except (ValueError, OSError):  # stream closed under us on kill
            box.append("")

    t_out = threading.Thread(target=_drain, args=(proc.stdout, out_box), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, err_box), daemon=True)
    t_out.start()
    t_err.start()

    # Feed the snippet (if any) and close stdin so the child isn't left waiting.
    if proc.stdin is not None:
        if stdin_data is not None:
            try:
                proc.stdin.write(stdin_data)
            except (BrokenPipeError, OSError):  # child died first — poll picks it up
                pass
        try:
            proc.stdin.close()
        except OSError:
            pass

    deadline = time.monotonic() + max(1, int(timeout_s))
    timed_out = False
    while proc.poll() is None:
        if cancel is not None and cancel():
            proc.kill()
            proc.wait()
            raise ToolCancelled()
        if time.monotonic() >= deadline:
            timed_out = True
            proc.kill()
            break
        time.sleep(_POLL_INTERVAL_S)

    proc.wait()
    t_out.join()
    t_err.join()
    out = out_box[0] if out_box else ""
    err = err_box[0] if err_box else ""
    if timed_out:
        err = err + f"\n[killed: exceeded {int(timeout_s)}s timeout]"
    return {
        "stdout": out,
        "stderr": err,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
    }

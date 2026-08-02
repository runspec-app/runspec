"""
codeexec.py — the **pure** helpers behind the ``run_python_sandboxed`` /
``run_sql_sandboxed`` agent tools: SQL read/write classification and code-input
normalisation. No subprocess, no wasmtime — every function here is pure and
unit-testable on any OS.

The actual execution happens in a WASM/WASI capability sandbox — see
:mod:`runspec_console.sandbox`. That module holds the (heavy, optional) runtime;
this one holds only the logic the autonomy gate and the dispatcher need without
importing wasmtime.

Sandbox posture (deliberate)
============================
``run_python_sandboxed`` executes untrusted Python inside a WASI CPython
interpreter under wasmtime with **no ambient authority**: only the current chat
session's workspace is preopened, there is no network, no host filesystem, no
subprocess, and no host environment — a real security boundary, not a policy the
code can bypass. See ``sandbox.py`` for the mechanism. ``run_sql_sandboxed`` runs
its statement in the same sandbox; a read is additionally opened ``mode=ro`` so it
cannot mutate even if :func:`classify_sql` was fooled. The confirm gate remains as
operator governance on top of the boundary.
"""

from __future__ import annotations

# SQL leading keywords that only read — everything else (INSERT/UPDATE/DELETE/
# CREATE/DROP/ALTER/REPLACE/PRAGMA-write/…) is treated as a write and thus
# confirm-gated. WITH … <SELECT> is a read; a writable CTE (WITH … DELETE) is not.
_SQL_READ_LEADERS = frozenset({"select", "explain", "with", "values"})


def classify_sql(query: str) -> str:
    """``"read"`` for a query that only reads (``SELECT``/``EXPLAIN``/read
    ``PRAGMA``/``VALUES``/``WITH … SELECT``), else ``"write"``.

    Conservative: anything it can't confidently call a read is a write, so the
    autonomy gate errs toward prompting. Used by ``_effective_tool_autonomy`` to
    let a read-only ``run_sql_sandboxed`` run autonomously while writes/DDL
    confirm-gate."""
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


def normalize_code(code: object) -> str | None:
    """Coerce the ``code`` argument to a single source string.

    Accepts a plain string or a **list of lines** — the latter lets the agent pass
    multi-line source without JSON-escaping every newline (``["import os",
    "print(1)"]`` instead of ``"import os\\nprint(1)"``). A list is joined with
    newlines; a non-list, non-string (or empty) returns ``None`` so the caller can
    report "no source received"."""
    if isinstance(code, str):
        return code or None
    if isinstance(code, (list, tuple)):
        parts = [str(line) for line in code]
        joined = "\n".join(parts)
        return joined or None
    return None

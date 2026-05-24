"""Launcher for chainlit that applies a Python 3.14 compatibility shim.

`chainlit.cli` calls ``nest_asyncio.apply()`` at import time, which replaces
``asyncio.Task`` with the pure-Python ``_PyTask``. On Python 3.14
``asyncio.current_task`` is bound to the C implementation, which only sees
C tasks and returns ``None`` for the Python tasks created after
nest_asyncio has been applied. That breaks ``sniffio.current_async_library()``,
which in turn breaks ``anyio.to_thread.run_sync`` — every starlette/fastapi
request that touches the threadpool then raises ``anyio.NoEventLoopError``
(so the chainlit UI never loads).

Rebinding ``asyncio.current_task`` and ``asyncio.all_tasks`` to the Python
implementations restores correct behaviour after ``nest_asyncio.apply()``.
"""

from __future__ import annotations

import sys


def _patch_for_py314_nest_asyncio() -> None:
    if sys.version_info < (3, 14):
        return
    import asyncio
    import asyncio.tasks as _t

    py_current = getattr(_t, "_py_current_task", None)
    py_all = getattr(_t, "_py_all_tasks", None)
    if py_current is not None:
        asyncio.current_task = _t.current_task = py_current
    if py_all is not None:
        asyncio.all_tasks = _t.all_tasks = py_all


def main() -> None:
    from chainlit.cli import cli  # side effect: nest_asyncio.apply()

    _patch_for_py314_nest_asyncio()
    cli(prog_name="chainlit")


if __name__ == "__main__":
    main()

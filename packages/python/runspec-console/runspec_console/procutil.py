"""Shared subprocess creation flags.

On Windows the console is launched *windowed* — the ``runspec-console-app``
gui-script runs under ``pythonw.exe``, which has no console of its own. When such
a process spawns a child that is itself a **console** program (``runspec.exe``,
``pip``, ``sh``, the sandbox worker, a local runnable, …), Windows allocates a
fresh console for the child, which flashes a terminal window on screen even
though nothing is wrong. Passing ``CREATE_NO_WINDOW`` suppresses that console;
captured stdout/stderr are unaffected.

``no_window_kwargs()`` returns the flag as ready-to-splat ``subprocess`` kwargs on
Windows and an empty dict everywhere else, so a call site can write
``subprocess.run(argv, **no_window_kwargs())`` unconditionally. It deliberately
does *not* cover the GUI file-manager launchers (``explorer /select`` etc.) —
those are meant to show a window.
"""

from __future__ import annotations

import subprocess


def no_window_kwargs() -> dict[str, int]:
    """``{"creationflags": CREATE_NO_WINDOW}`` on Windows, ``{}`` elsewhere.

    ``subprocess.CREATE_NO_WINDOW`` only exists on Windows, so its presence *is*
    the platform check: ``getattr`` keeps the module importable and the helper a
    no-op off Windows (an empty splat leaves the call unchanged)."""
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flag} if flag else {}

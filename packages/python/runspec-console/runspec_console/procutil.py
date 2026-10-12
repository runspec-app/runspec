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

import os
import signal
import subprocess


def no_window_kwargs() -> dict[str, int]:
    """``{"creationflags": CREATE_NO_WINDOW}`` on Windows, ``{}`` elsewhere.

    ``subprocess.CREATE_NO_WINDOW`` only exists on Windows, so its presence *is*
    the platform check: ``getattr`` keeps the module importable and the helper a
    no-op off Windows (an empty splat leaves the call unchanged)."""
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flag} if flag else {}


def run_with_deadline(
    argv: list[str], timeout: float, **kwargs: object
) -> tuple[int | None, str, str]:
    """Run ``argv`` capturing text output; ``(None, out, err)`` on timeout.

    ``subprocess.run(timeout=)`` kills only the child, then on Windows waits for
    its output pipes to close. A grandchild that inherited them — the jump
    hop's ``ProxyCommand`` ssh, or a ``connect`` relay it starts — keeps them
    open, so the call could block forever. This kills the whole process tree
    and stops waiting after a short grace.
    """
    posix = os.name != "nt"
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=posix,
        **no_window_kwargs(),
        **kwargs,  # type: ignore[arg-type]
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        pass
    _kill_tree(proc)
    try:
        out, err = proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        out, err = "", ""  # something still holds the pipes; give up on it
    return None, out or "", err or ""


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                **no_window_kwargs(),
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001 — fall back to the child alone
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass

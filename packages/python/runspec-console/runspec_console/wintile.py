"""wintile.py — arrange a batch of freshly launched windows on screen.

Used by ``Bridge.launch_terminals`` to lay several PuTTY windows out in a grid
on the monitor the console is on. Two halves:

* ``tile_rects(n, area, layout=…)`` — pure layout maths (unit-tested on any
  platform): a near-square ``grid``, a ``compact`` grid of terminal-sized
  windows packed from the top-left, side-by-side ``columns``, stacked
  ``rows``, or a ``cascade``. A grid/columns/rows layout whose cells would be
  too small to read a terminal in falls back (columns/rows → grid → cascade).
* ``tile_process_windows(pids, …)`` — Windows-only, best-effort: wait for each
  process's top-level window (by window class) to appear, then move them into
  the rects. ``ctypes`` only, no pywin32. Never raises; a window that never
  shows (a crashed PuTTY, a slow host-key prompt) is simply left where Windows
  put it.
"""

from __future__ import annotations

import contextlib
import math
import sys
import time
from collections.abc import Sequence

Rect = tuple[int, int, int, int]  # x, y, width, height

# Below this a terminal cell is too small to use — switch to a cascade.
MIN_TILE_W = 480
MIN_TILE_H = 260
# Cascade step between successive windows.
CASCADE_STEP = 32

# A compact-grid window at 100% scaling — about a 100×30 terminal in PuTTY's
# default font. Scaled by the monitor's DPI so it looks the same at 150%.
COMPACT_W = 820
COMPACT_H = 520

LAYOUTS = ("grid", "compact", "columns", "rows", "cascade")


def grid_shape(n: int) -> tuple[int, int]:
    """``(cols, rows)`` for ``n`` windows — near-square, wider than tall."""
    if n <= 0:
        return (0, 0)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return (cols, rows)


def _grid(n: int, area: Rect, cols: int, rows: int) -> list[Rect]:
    """``n`` rects in a ``cols`` × ``rows`` grid filling ``area`` edge to edge.
    A short last row is stretched across the full width; the last cell in a
    row/column takes any rounding remainder."""
    ax, ay, aw, ah = area
    cell_h = ah // rows
    rects: list[Rect] = []
    for i in range(n):
        r, c = divmod(i, cols)
        in_row = min(cols, n - r * cols)
        w = aw // in_row
        x = ax + c * w
        width = (aw - c * w) if c == in_row - 1 else w
        y = ay + r * cell_h
        height = (ah - r * cell_h) if r == rows - 1 else cell_h
        rects.append((x, y, width, height))
    return rects


def _cascade(n: int, area: Rect, min_w: int, min_h: int) -> list[Rect]:
    """Each window half the area, offset by ``CASCADE_STEP``, wrapping back to
    the top-left when the next one would run off the area."""
    ax, ay, aw, ah = area
    w = max(min(aw, min_w), aw // 2)
    h = max(min(ah, min_h), ah // 2)
    span_x = max(aw - w, 0)
    span_y = max(ah - h, 0)
    steps = max(1, min(span_x, span_y) // CASCADE_STEP + 1)
    out: list[Rect] = []
    for i in range(n):
        k = i % steps
        out.append((ax + k * CASCADE_STEP, ay + k * CASCADE_STEP, w, h))
    return out


def tile_rects(
    n: int,
    area: Rect,
    *,
    layout: str = "grid",
    min_w: int = MIN_TILE_W,
    min_h: int = MIN_TILE_H,
    scale: float = 1.0,
) -> list[Rect]:
    """One rect per window, in launch order, inside ``area`` (x, y, w, h).

    ``layout``: ``grid`` (near-square, wider than tall), ``compact`` (the
    same grid shape, but each window ``COMPACT_W`` × ``COMPACT_H`` × ``scale``
    packed from the top-left corner, shrunk only when that won't fit — the
    windows are the same size on any monitor shape), ``columns`` (all side
    by side, full height — suits an ultrawide), ``rows`` (all stacked, full
    width) or ``cascade``. When the chosen layout's cells would be smaller
    than ``min_w`` × ``min_h`` it falls back: columns/rows → grid → cascade.
    An unknown layout is treated as ``grid``.
    """
    if n <= 0:
        return []
    _ax, _ay, aw, ah = area
    if layout == "cascade":
        return _cascade(n, area, min_w, min_h)
    if layout == "compact":
        cols, rows = grid_shape(n)
        cw = min(int(COMPACT_W * scale), aw // cols)
        ch = min(int(COMPACT_H * scale), ah // rows)
        if cw < min_w or ch < min_h:
            return _cascade(n, area, min_w, min_h)
        ax, ay = area[0], area[1]
        return [(ax + (i % cols) * cw, ay + (i // cols) * ch, cw, ch) for i in range(n)]
    if layout == "columns" and aw // n >= min_w:
        return _grid(n, area, n, 1)
    if layout == "rows" and ah // n >= min_h:
        return _grid(n, area, 1, n)
    cols, rows = grid_shape(n)
    if aw // cols >= min_w and ah // rows >= min_h:
        return _grid(n, area, cols, rows)
    return _cascade(n, area, min_w, min_h)


# ── Windows ──────────────────────────────────────────────────────────────────


def _work_area() -> Rect | None:
    """The work area (screen minus taskbar) of the monitor the foreground
    window — the console, at launch time — is on. ``None`` off Windows/on
    error."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        hwnd = user32.GetForegroundWindow()
        mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return None
        rc = info.rcWork
        return (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top)
    except Exception:  # noqa: BLE001 — best-effort
        return None


def _find_windows(pids: Sequence[int], window_class: str) -> dict[int, int]:
    """``{pid: hwnd}`` for each pid's visible top-level window of
    ``window_class`` (PuTTY's main window class is ``"PuTTY"``)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    want = set(pids)
    found: dict[int, int] = {}
    buf = ctypes.create_unicode_buffer(256)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):  # type: ignore[no-untyped-def]
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in want and pid.value not in found:
            user32.GetClassNameW(hwnd, buf, 256)
            if buf.value == window_class:
                found[pid.value] = hwnd
        return True

    user32.EnumWindows(_cb, 0)
    return found


def tile_process_windows(
    pids: Sequence[int],
    *,
    layout: str = "grid",
    window_class: str = "PuTTY",
    timeout: float = 15.0,
    poll: float = 0.25,
) -> int:
    """Wait (up to ``timeout``) for each pid's window, then arrange them on the
    console's monitor in ``pids`` order using ``layout`` (see ``tile_rects``). Returns how many were moved.

    Windows-only and best-effort — returns 0 elsewhere or on any error. Runs
    for up to ``timeout`` seconds, so call it from a background thread."""
    if sys.platform != "win32" or not pids:
        return 0
    try:
        import ctypes

        user32 = ctypes.windll.user32
        # Move windows in physical pixels so a scaled (125%/150%) display lines
        # up; per-thread, so the rest of the app's DPI mode is untouched.
        # PER_MONITOR_AWARE_V2; absent before Windows 10 1607.
        with contextlib.suppress(Exception):
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        area = _work_area()
        if area is None:
            return 0
        deadline = time.monotonic() + timeout
        found: dict[int, int] = {}
        while True:
            found = _find_windows(pids, window_class)
            if len(found) == len(set(pids)) or time.monotonic() >= deadline:
                break
            time.sleep(poll)
        ordered = [found[p] for p in pids if p in found]
        scale = 1.0
        if ordered:
            # The thread is per-monitor DPI aware, so this is the real DPI of
            # the monitor the first window opened on (absent before 1607).
            with contextlib.suppress(Exception):
                dpi = int(user32.GetDpiForWindow(ordered[0]))
                if dpi > 0:
                    scale = dpi / 96
        rects = tile_rects(len(ordered), area, layout=layout, scale=scale)
        SW_RESTORE = 9
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        moved = 0
        for hwnd, (x, y, w, h) in zip(ordered, rects):
            if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
            if user32.SetWindowPos(
                hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE
            ):
                moved += 1
        return moved
    except Exception:  # noqa: BLE001 — best-effort, never break a launch
        return 0

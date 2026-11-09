"""termsend.py — type into PuTTY windows the console opened (Send to terminals).

The *Send to terminals* bar lets a person type one command and have it typed
into many PuTTY windows at once, the way PuTTY Command Sender / MTPuTTY do it:
each character is posted to the window as a ``WM_CHAR`` message, so PuTTY
handles it exactly as a key press (the window doesn't need focus). Human-only by
design: nothing here is an agent tool or an MCP server tool.

Two halves:

* Pure, unit-tested on any platform: ``keystrokes(text, enter=…)`` and
  ``key_events(name)`` turn text or a named key into ``(message, wParam,
  lParam)`` tuples.
* Windows-only, best-effort: ``post(hwnd, events)``, ``is_window(hwnd)``,
  ``focus(hwnd)`` (``ctypes`` only, no pywin32). They never raise; elsewhere
  they report failure.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Sequence

Event = tuple[int, int, int]  # message, wParam, lParam

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102

# Longest text one send may type (characters). A pasted script is fine; a
# runaway paste of a log file is not.
MAX_TEXT = 8192

# Named keys the bar has buttons for. Control characters go as WM_CHAR (PuTTY
# sends the byte as typed); cursor keys need WM_KEYDOWN/UP with the scan code
# and the extended-key bit, or PuTTY reads them as numeric-keypad keys.
_CHAR_KEYS = {
    "enter": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "backspace": 0x08,
    "ctrl-c": 0x03,
    "ctrl-d": 0x04,
    "ctrl-z": 0x1A,
    "ctrl-l": 0x0C,
    "ctrl-r": 0x12,
}
# name -> (virtual-key code, scan code)
_CURSOR_KEYS = {
    "up": (0x26, 0x48),
    "down": (0x28, 0x50),
    "left": (0x25, 0x4B),
    "right": (0x27, 0x4D),
}
KEY_NAMES = tuple(_CHAR_KEYS) + tuple(_CURSOR_KEYS)


def keystrokes(text: str, *, enter: bool = True) -> list[Event]:
    """``WM_CHAR`` events that type ``text`` (then Enter when ``enter``).

    Line breaks (``\\n``, ``\\r\\n``, ``\\r``) become Enter (``\\r``), so a
    pasted multi-line block runs line by line, as it would pasted into PuTTY.
    Characters outside the Basic Multilingual Plane go as a UTF-16 surrogate
    pair (PuTTY's window is Unicode). Raises ``ValueError`` past ``MAX_TEXT``.
    """
    if len(text) > MAX_TEXT:
        raise ValueError(f"text is longer than {MAX_TEXT} characters")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[Event] = []
    for ch in text:
        if ch == "\n":
            out.append((WM_CHAR, 0x0D, 1))
            continue
        code = ord(ch)
        if code > 0xFFFF:
            code -= 0x10000
            out.append((WM_CHAR, 0xD800 + (code >> 10), 1))
            out.append((WM_CHAR, 0xDC00 + (code & 0x3FF), 1))
        else:
            out.append((WM_CHAR, code, 1))
    if enter:
        out.append((WM_CHAR, 0x0D, 1))
    return out


def key_events(name: str) -> list[Event]:
    """Events for one named key (``KEY_NAMES``). Raises ``ValueError`` for an
    unknown name."""
    key = name.strip().lower()
    if key in _CHAR_KEYS:
        return [(WM_CHAR, _CHAR_KEYS[key], 1)]
    if key in _CURSOR_KEYS:
        vk, scan = _CURSOR_KEYS[key]
        down = 1 | (scan << 16) | (1 << 24)  # repeat 1, scan code, extended key
        up = down | (1 << 30) | (1 << 31)  # previous state down, transition up
        return [(WM_KEYDOWN, vk, down), (WM_KEYUP, vk, up)]
    raise ValueError(f"unknown key {name!r}")


# ── Windows half ─────────────────────────────────────────────────────────────


def supported() -> bool:
    """Whether this machine can type into windows (Windows only)."""
    return sys.platform == "win32"


def post(hwnd: int, events: Sequence[Event]) -> bool:
    """Post ``events`` to ``hwnd`` in order. ``False`` when the window is gone,
    a post fails, or this isn't Windows."""
    if not supported() or not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        if not user32.IsWindow(hwnd):
            return False
        for msg, wparam, lparam in events:
            # lParam is signed on the C side; keep the up-transition bits.
            lp = lparam - (1 << 32) if lparam >= (1 << 31) else lparam
            if not user32.PostMessageW(hwnd, msg, wparam, lp):
                return False
        return True
    except Exception:  # noqa: BLE001 — best-effort
        return False


def is_window(hwnd: int) -> bool:
    """Whether ``hwnd`` is still an open window (always ``False`` off Windows)."""
    if not supported() or not hwnd:
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.IsWindow(hwnd))
    except Exception:  # noqa: BLE001
        return False


def focus(hwnd: int) -> bool:
    """Restore ``hwnd`` if minimised and bring it to the front."""
    if not is_window(hwnd):
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        with contextlib.suppress(Exception):
            user32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001
        return False

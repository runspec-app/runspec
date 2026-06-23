"""winkeys.py — inject the Voice access start chord (Windows-only).

The chat composer's **Speak** button wants to *start* Windows Voice access
dictation, not just focus the box. JavaScript in the WebView2 sandbox can't
synthesize an OS-level hotkey — especially one using the Windows key — so the
button routes through the bridge to here, where ``SendInput`` injects the chord
at the OS level.

``send_voice_access_hotkey()`` presses the documented Voice access start chord
(**Ctrl+Win+S**). Best-effort and **fail-soft**: any error or non-Windows is a
no-op returning ``False`` (the operator can still dictate by starting Voice
access from its own bar). The chord is a single named constant so it's a one-line
change if a given build documents a different combo.

Mechanism: ``user32.SendInput`` with paired key-down then key-up events. We press
Ctrl and Win *before* S and release in reverse order, so the Win key never goes
down-and-up alone — that ordering avoids accidentally popping the Start menu.
"""

from __future__ import annotations

import sys

# Virtual-key codes (winuser.h).
_VK_CONTROL = 0x11
_VK_LWIN = 0x5B
_VK_S = 0x53

# The chord Windows documents to start Voice access dictation. Order matters:
# modifiers first so they're held while the action key is pressed.
VOICE_ACCESS_HOTKEY: tuple[int, ...] = (_VK_CONTROL, _VK_LWIN, _VK_S)


def send_voice_access_hotkey() -> bool:
    """Inject the Voice access start chord (Ctrl+Win+S). Returns True if the chord
    was sent, False on non-Windows or any failure (never raises)."""
    return _send_chord(VOICE_ACCESS_HOTKEY)


def _send_chord(vks: tuple[int, ...]) -> bool:
    if sys.platform != "win32" or not vks:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        ULONG_PTR = ctypes.c_size_t  # UINT_PTR — 32/64-bit safe
        _INPUT_KEYBOARD = 1
        _KEYEVENTF_KEYUP = 0x0002

        class _KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("ki", _KEYBDINPUT)]

        class _INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

        user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        )
        user32.SendInput.restype = wintypes.UINT

        def _key(vk: int, *, up: bool) -> _INPUT:
            ki = _KEYBDINPUT(vk, 0, _KEYEVENTF_KEYUP if up else 0, 0, 0)
            return _INPUT(_INPUT_KEYBOARD, _INPUTUNION(ki=ki))

        # Press in order, release in reverse — Win never down/up alone.
        events = [_key(vk, up=False) for vk in vks]
        events += [_key(vk, up=True) for vk in reversed(vks)]
        arr = (_INPUT * len(events))(*events)
        sent = user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
        return sent == len(events)
    except Exception:
        return False

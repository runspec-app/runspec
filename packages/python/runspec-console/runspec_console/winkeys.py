"""winkeys.py — toggle Windows Voice access listening (Windows-only).

The chat composer's **Speak** button wants to wake Windows Voice access so it
listens and dictates into the focused prompt box, and put it back to sleep when
the operator sends. JavaScript in the WebView2 sandbox can't synthesize an
OS-level hotkey, so the button routes through the bridge to here, where
``SendInput`` injects the chord at the OS level.

``send_voice_access_toggle()`` presses the documented Voice access wake/sleep
chord (**Alt+Shift+B**) — the *same* chord toggles both ways, so the caller
tracks the intended state and presses it to flip. Best-effort and **fail-soft**:
any error or non-Windows is a no-op returning ``False`` (the operator can still
drive Voice access from its own bar). The chord is a single named constant so
it's a one-line change if a build documents a different combo (e.g. Alt+Shift+C
for mic on/off).

Mechanism: ``user32.SendInput``, sent **one event at a time with a short hold
between them** and tagged with each key's **hardware scan code**
(``MapVirtualKey``) so it mimics a real keypress. An instantaneous six-event
batch with no scan codes is honoured by *some* listeners but not by Voice access
(manual Alt+Shift+B works; a raw batch did not). Modifiers are pressed first and
released last, so the action key fires while they're held.
"""

from __future__ import annotations

import sys

# Virtual-key codes (winuser.h).
_VK_SHIFT = 0x10
_VK_MENU = 0x12  # Alt
_VK_B = 0x42

# The chord Windows documents to wake/sleep Voice access listening. Order matters:
# modifiers first so they're held while the action key is pressed.
VOICE_ACCESS_TOGGLE: tuple[int, ...] = (_VK_MENU, _VK_SHIFT, _VK_B)


def send_voice_access_toggle() -> bool:
    """Inject the Voice access wake/sleep chord (Alt+Shift+B). Returns True if the
    chord was sent, False on non-Windows or any failure (never raises)."""
    return _send_chord(VOICE_ACCESS_TOGGLE)


def _send_chord(vks: tuple[int, ...], *, hold_ms: int = 30) -> bool:
    if sys.platform != "win32" or not vks:
        return False
    try:
        import ctypes
        import time
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        ULONG_PTR = ctypes.c_size_t  # UINT_PTR — 32/64-bit safe
        _INPUT_KEYBOARD = 1
        _KEYEVENTF_KEYUP = 0x0002
        _MAPVK_VK_TO_VSC = 0

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
        user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
        user32.MapVirtualKeyW.restype = wintypes.UINT

        def _send_one(vk: int, *, up: bool) -> int:
            # Tag with the hardware scan code so listeners that key off scan codes
            # (Voice access among them) see a real-looking keypress.
            scan = user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC)
            ki = _KEYBDINPUT(vk, scan, _KEYEVENTF_KEYUP if up else 0, 0, 0)
            inp = _INPUT(_INPUT_KEYBOARD, _INPUTUNION(ki=ki))
            return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

        # Press in order, release in reverse — modifiers held over the action key —
        # one event at a time with a brief hold between, so the combo registers
        # like real typing rather than a single instantaneous batch.
        seq = [(vk, False) for vk in vks] + [(vk, True) for vk in reversed(vks)]
        sent = 0
        for vk, up in seq:
            sent += _send_one(vk, up=up)
            time.sleep(hold_ms / 1000.0)
        return sent == len(seq)
    except Exception:
        return False

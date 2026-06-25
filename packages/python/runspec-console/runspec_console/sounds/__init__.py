"""sounds — best-effort audio cues for the console (Windows-only).

The operator often minimises the console or focuses another app; these cues let
them stay aware of what's happening — an agent prompt waiting for confirmation,
a trigger firing, a run finishing, a message arriving. Playback is **backend**
(stdlib ``winsound``) rather than frontend HTML5 audio, because a minimised /
backgrounded WebView2 throttles in-page audio, whereas ``winsound.PlaySound(...,
SND_ASYNC)`` plays from the host process regardless of window state.

Mirrors ``notify.py``'s safe shape: an ``available()`` gate, every entry point
swallows exceptions and never raises, a pure no-op off Windows, and no new
runtime dependency (``winsound`` is stdlib). The cue ``.wav`` files in this
directory are original synthesised tones (see ``_generate.py``) — fully ours,
no licensing exposure.

A *category* (e.g. ``"confirm"``) is mapped by config to a *token*: ``"synth:<name>"``
(a bundled wav), ``"system:<Alias>"`` (a Windows system sound), or ``"off"``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .. import config as config_mod

logger = logging.getLogger("runspec.console.sounds")

# The bundled wav cue names (the files alongside this module). Mirrors the set
# produced by _generate.py.
KNOWN_SOUNDS = ("confirm", "success", "error", "stopped", "trigger", "reply", "message")

# The event categories config maps to a sound token (see config.DEFAULT_SOUND_MAP).
CATEGORIES = (
    "confirm",
    "trigger",
    "completion_success",
    "completion_error",
    "completion_stopped",
    "reply",
    "message",
)


def categories() -> tuple[str, ...]:
    """Canonical category keys, for the Settings UI and tests."""
    return CATEGORIES


def available() -> tuple[bool, str]:
    """Whether audio cues can be played here. ``(False, reason)`` off-Windows or
    if ``winsound`` is somehow unavailable; ``(True, "")`` otherwise. Cheap and
    safe to call per event."""
    if sys.platform != "win32":
        return (False, "Audio cues require Windows")
    try:
        import winsound  # noqa: F401
    except Exception:
        return (False, "winsound unavailable")
    return (True, "")


def resolve_target(token: str | None) -> tuple[str, str] | None:
    """Parse a config token into ``("synth", name)`` / ``("system", alias)``, or
    ``None`` for off/blank/invalid. Pure — unit-testable on any OS, never raises."""
    if not isinstance(token, str):
        return None
    token = token.strip()
    if not token or token == "off":
        return None
    if token.startswith("synth:"):
        name = token[len("synth:") :].strip()
        return ("synth", name) if name in KNOWN_SOUNDS else None
    if token.startswith("system:"):
        alias = token[len("system:") :].strip()
        return ("system", alias) if alias else None
    return None


def _sound_path(name: str) -> Path | None:
    p = Path(__file__).parent / f"{name}.wav"
    return p if p.exists() else None


def play(category: str, *, force: bool = False) -> bool:
    """Play the configured cue for ``category``. Returns True if a sound was
    started. ``force`` bypasses the master-enable + 'off' gates (the Test button),
    falling back to the built-in default for the category. Never raises."""
    try:
        ok, _ = available()
        if not ok:
            return False
        if not force and not config_mod.sound_enabled():
            return False
        target = resolve_target(config_mod.sound_choice(category))
        if target is None and force:
            target = resolve_target(config_mod.DEFAULT_SOUND_MAP.get(category, ""))
        if target is None:
            return False
        import winsound

        flags = winsound.SND_ASYNC | winsound.SND_NODEFAULT
        kind, value = target
        if kind == "synth":
            path = _sound_path(value)
            if path is None:
                return False
            winsound.PlaySound(str(path), winsound.SND_FILENAME | flags)
        else:  # system alias
            winsound.PlaySound(value, winsound.SND_ALIAS | flags)
        return True
    except Exception:
        logger.debug("play sound failed (category=%s)", category, exc_info=True)
        return False

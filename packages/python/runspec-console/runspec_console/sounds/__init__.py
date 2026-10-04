"""sounds — best-effort audio cues for the console (Windows-only).

The operator often minimises the console or focuses another app; these cues let
them stay aware of what's happening — an agent prompt waiting for confirmation,
a trigger firing, a run finishing, a message arriving, an SSH host or chat room
dropping. Playback is **backend** rather than frontend HTML5 audio, because a
minimised / backgrounded WebView2 throttles in-page audio, whereas playing from
the host process works regardless of window state.

Mirrors ``notify.py``'s safe shape: an ``available()`` gate, every entry point
swallows exceptions and never raises, a pure no-op off Windows, and no new
runtime dependency. The cue ``.wav`` files in this directory are original
synthesised tones (see ``_generate.py``) — fully ours, no licensing exposure.

A *category* (e.g. ``"confirm"``) resolves to a cue in three steps:

1. an explicit config token — ``"synth:<name>"`` (bundled wav),
   ``"system:<Alias>"`` (Windows system sound), ``"file:<path>"`` (an operator's
   own ``.wav``/``.mp3``), ``"sound:<id>"`` (a **named sound** from the
   ``runspec_sounds.toml`` registry — see :mod:`sounds_registry`), or ``"off"``;
2. else a **sound pack**: ``[console.sounds] pack = "<dir>"`` names a folder of
   category-named files (``confirm.wav``, ``host_down.mp3``, …) — one folder
   pick installs a whole personality;
3. else the built-in default (``config.DEFAULT_SOUND_MAP``).

Playback of file-based cues (bundled synth + ``file:``/pack) goes through the
Windows MCI API (``winmm`` via stdlib ``ctypes``): async, plays mp3 as well as
wav, and honours the ``[console.sounds] volume`` setting (0–100) — something
``winsound`` cannot do. ``system:`` aliases stay on ``winsound`` (no volume).
"""

from __future__ import annotations

import itertools
import logging
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .. import config as config_mod

logger = logging.getLogger("runspec.console.sounds")

# The bundled wav cue names (the files alongside this module). Mirrors the set
# produced by _generate.py.
KNOWN_SOUNDS = (
    "confirm",
    "success",
    "error",
    "stopped",
    "trigger",
    "reply",
    "message",
    "host_up",
    "host_down",
    "room_up",
    "room_down",
    "waiting",
)

# The event categories config maps to a sound token (see config.DEFAULT_SOUND_MAP).
CATEGORIES = (
    "confirm",
    "trigger",
    "completion_success",
    "completion_error",
    "completion_stopped",
    "reply",
    "message",
    "host_up",
    "host_down",
    "room_up",
    "room_down",
    "waiting",
)

# Extensions a file:<path> token / pack file may use, in pack-lookup preference
# order (wav first — it starts faster under MCI than mp3).
FILE_EXTS = (".wav", ".mp3")


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


def resolve_target(
    token: str | None, *, registry: dict[str, str] | None = None
) -> tuple[str, str] | None:
    """Parse a config token into ``("synth", name)`` / ``("system", alias)`` /
    ``("file", path)``, or ``None`` for off/blank/invalid. Pure — unit-testable
    on any OS, never raises. A ``file:`` token must carry a ``.wav``/``.mp3``
    suffix; existence is checked at play time (a missing file is a silent no-op,
    not an error — the operator may have unplugged a drive).

    A ``sound:<id>`` token names a registered sound; it resolves through
    ``registry`` (``{id: path}``). ``registry`` defaults to the live
    ``runspec_sounds.toml`` registry, but tests (and any pure caller) may inject
    one to keep this function OS/config-free. An unknown id → ``None`` (silent)."""
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
    if token.startswith("file:"):
        path = token[len("file:") :].strip()
        if path and Path(path).suffix.lower() in FILE_EXTS:
            return ("file", path)
        return None
    if token.startswith("sound:"):
        sid = token[len("sound:") :].strip()
        reg = registry if registry is not None else _registry_map()
        path = reg.get(sid) or ""
        if path and Path(path).suffix.lower() in FILE_EXTS:
            return ("file", path)
        return None
    return None


def _registry_map() -> dict[str, str]:
    """The named-sound registry (``{id: path}``), or ``{}`` on any failure —
    isolated so ``resolve_target`` stays a pure default and never raises."""
    try:
        from .. import sounds_registry

        return sounds_registry.registry_map()
    except Exception:
        return {}


def resolve_playable(identifier: str) -> str | None:
    """Map a friendly ``identifier`` — a registry sound id, a bundled synth name,
    or an already-formed token (``sound:``/``synth:``/``system:``/``file:``) — to a
    playable token, or ``None`` when nothing matches. This is what the ``play_sound``
    agent tool, a trigger's / schedule's ``sound`` field, and the Settings preview
    accept, so the operator/agent name a sound without the token prefix.

    Precedence: a full token is used as-is; then a registry id (``sound:<id>``);
    then a bundled synth name (``synth:<name>``). Never raises."""
    ident = str(identifier or "").strip()
    if not ident or ident == "off":
        return None
    if ident.startswith(("sound:", "synth:", "system:", "file:")):
        return ident
    try:
        if ident in _registry_map():
            return f"sound:{ident}"
    except Exception:
        pass
    if ident in KNOWN_SOUNDS:
        return f"synth:{ident}"
    return None


def playable_options() -> list[dict[str, str]]:
    """The named sounds available for the agent-tool enum / UI pickers:
    ``[{id, label, kind}]`` — every registry sound (``kind="registered"``) followed
    by the bundled synth cues (``kind="builtin"``). Registry ids win a name clash
    (listed first). Never raises."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        from .. import sounds_registry

        for s in sounds_registry.list_sounds():
            if s.id in seen:
                continue
            seen.add(s.id)
            out.append({"id": s.id, "label": s.label or s.id, "kind": "registered"})
    except Exception:
        pass
    for name in KNOWN_SOUNDS:
        if name not in seen:
            out.append({"id": name, "label": f"{name} (built-in)", "kind": "builtin"})
    return out


def play_identifier(identifier: str, *, force: bool = False) -> bool:
    """Play a sound named by a friendly ``identifier`` (see :func:`resolve_playable`).
    Respects the master ``[console.sounds] enabled`` toggle unless ``force`` (the
    Settings preview). Returns True if a sound started. Never raises."""
    token = resolve_playable(identifier)
    if token is None:
        return False
    return play("__named__", force=force, token=token)


def pack_lookup(pack_dir: str | Path, category: str) -> Path | None:
    """The pack file for a category: ``<dir>/<category>.wav`` (preferred) or
    ``.mp3``. Pure filesystem probe — never raises, ``None`` when absent."""
    try:
        d = Path(pack_dir)
        if not str(pack_dir).strip() or not d.is_dir():
            return None
        for ext in FILE_EXTS:
            p = d / f"{category}{ext}"
            if p.is_file():
                return p
    except Exception:
        pass
    return None


def pack_coverage(pack_dir: str | Path) -> dict[str, str]:
    """Which categories a pack folder covers: ``{category: filename}``. For the
    Settings UI's "covers N of M" hint. Never raises."""
    out: dict[str, str] = {}
    for cat in CATEGORIES:
        p = pack_lookup(pack_dir, cat)
        if p is not None:
            out[cat] = p.name
    return out


class ConnectionCueGate:
    """Decides when a connection-state flip earns an audio cue.

    Rules: the first observation of a key (startup / a freshly added host) is
    recorded silently; after that, only a genuine flip cues, and no key cues
    more than once per ``cooldown`` seconds — so a flapping host or a room
    stuck in a reconnect loop can't become a metronome. Thread-safe (probe
    workers and room threads call in concurrently); pure logic otherwise, so
    it's unit-testable with an injected clock."""

    def __init__(self, cooldown: float = 30.0) -> None:
        self._cooldown = cooldown
        self._lock = threading.Lock()
        self._state: dict[
            str, tuple[bool, float]
        ] = {}  # key → (connected, last cue ts)

    def note(self, key: str, connected: bool, now: float) -> bool:
        """Record an observation; True when a cue should play for this flip."""
        with self._lock:
            prev = self._state.get(key)
            if prev is None:
                self._state[key] = (connected, float("-inf"))
                return False
            prev_connected, last_cue = prev
            if connected == prev_connected:
                return False
            if now - last_cue < self._cooldown:
                self._state[key] = (
                    connected,
                    last_cue,
                )  # flip recorded, cue suppressed
                return False
            self._state[key] = (connected, now)
            return True


class WaitingLoop:
    """The looping "still waiting on you" cue.

    While something in the UI needs the operator (a pending Approve/Deny, an
    unclaimed queue caller — i.e. a *count* badge on a tab) the frontend holds
    this loop active, and it plays the ``waiting`` category every ``interval()``
    seconds until the badge clears — a gentle repeating ping in the spirit of a
    video call's "connection lost" sonar, not an alarm. The first ping comes one
    interval after activation, since the event that raised the badge already
    played its own one-shot cue (``confirm`` / ``message``).

    The frontend re-asserts ``set_active(True)`` periodically; if those stop
    (the window reloaded or crashed mid-wait) the loop goes quiet on its own
    after ``lease`` seconds, so a lost "clear" can't leave it pinging forever.

    ``play``/``interval``/``clock`` are injected so the timing is unit-testable
    without audio. One daemon thread, started lazily on first activation. Never
    raises."""

    def __init__(
        self,
        play: Callable[[], object],
        interval: Callable[[], float],
        *,
        lease: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._play = play
        self._interval = interval
        self._lease = lease
        self._clock = clock
        self._cond = threading.Condition()
        self._active = False
        self._asserted = 0.0  # clock() of the last set_active(True)
        self._gen = 0  # bumped on every off→on, so a re-activation restarts the wait
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        with self._cond:
            return self._active and not self._expired()

    def _expired(self) -> bool:
        return self._clock() - self._asserted > self._lease

    def set_active(self, active: bool) -> None:
        try:
            with self._cond:
                if active:
                    if not self._active or self._expired():
                        self._gen += 1
                    self._active = True
                    self._asserted = self._clock()
                    if self._thread is None or not self._thread.is_alive():
                        self._thread = threading.Thread(
                            target=self._run, name="rs-waiting-cue", daemon=True
                        )
                        self._thread.start()
                else:
                    self._active = False
                self._cond.notify_all()
        except Exception:
            logger.debug("waiting cue set_active failed", exc_info=True)

    def _safe_interval(self) -> float:
        try:
            return max(0.01, float(self._interval()))
        except Exception:
            return 8.0

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._active:
                    self._cond.wait()
                gen = self._gen
                # Wait one interval; a deactivate (or re-activation) wakes us early.
                self._cond.wait(timeout=self._safe_interval())
                if not self._active or gen != self._gen:
                    continue
                if self._expired():
                    self._active = False  # lease lapsed — the UI stopped asserting
                    continue
            try:
                self._play()
            except Exception:
                logger.debug("waiting cue play failed", exc_info=True)


def _sound_path(name: str) -> Path | None:
    p = Path(__file__).parent / f"{name}.wav"
    return p if p.exists() else None


def _resolve_effective(category: str) -> tuple[str, str] | None:
    """Resolution order: explicit config token (an explicit ``off`` silences the
    category even with a pack) → pack file → built-in default."""
    raw = config_mod.sound_choice_set(category)
    if raw is not None:
        return resolve_target(raw)
    pack = config_mod.sound_pack()
    if pack:
        p = pack_lookup(pack, category)
        if p is not None:
            return ("file", str(p))
    return resolve_target(config_mod.DEFAULT_SOUND_MAP.get(category, ""))


# ── playback (Windows only past this point) ───────────────────────────────────

_mci_seq = itertools.count(1)


def _mci(command: str) -> tuple[int, str]:
    """One MCI command; ``(rc, returned_text)``. rc 0 = success."""
    import ctypes

    buf = ctypes.create_unicode_buffer(160)
    rc = ctypes.windll.winmm.mciSendStringW(command, buf, 159, None)  # type: ignore[attr-defined]
    return int(rc), buf.value


def _play_file_mci(path: str, volume: int) -> bool:
    """Async file playback via MCI with per-sound volume. Opening as
    ``mpegvideo`` (DirectShow) handles wav *and* mp3 and supports ``setaudio``
    volume; a plain open is the fallback (no volume). The alias is closed by a
    daemon timer after the clip's reported length so handles don't leak."""
    alias = f"rs_cue_{next(_mci_seq)}"
    rc, _ = _mci(f'open "{path}" type mpegvideo alias {alias}')
    if rc != 0:
        rc, _ = _mci(f'open "{path}" alias {alias}')
        if rc != 0:
            return False
    else:
        _mci(f"setaudio {alias} volume to {max(0, min(100, volume)) * 10}")
    rc, _ = _mci(f"play {alias}")
    if rc != 0:
        _mci(f"close {alias}")
        return False
    _, length_ms = _mci(f"status {alias} length")
    try:
        delay = min(int(length_ms), 30_000) / 1000.0 + 0.5
    except (TypeError, ValueError):
        delay = 30.5
    t = threading.Timer(delay, lambda: _mci(f"close {alias}"))
    t.daemon = True
    t.start()
    return True


def _play_path(path: str, volume: int) -> bool:
    """A file-based cue: MCI first (mp3 + volume), winsound fallback for wav."""
    if _play_file_mci(path, volume):
        return True
    if Path(path).suffix.lower() != ".wav":
        return False
    import winsound

    winsound.PlaySound(
        path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
    )
    return True


def play(category: str, *, force: bool = False, token: str | None = None) -> bool:
    """Play the configured cue for ``category``. Returns True if a sound was
    started. ``force`` bypasses the master-enable + 'off' gates (the Test
    button), falling back to the built-in default for the category. ``token``
    plays a specific token instead of the configured one (the Settings preview
    button, so an unsaved selection is auditionable). Never raises."""
    try:
        ok, _ = available()
        if not ok:
            return False
        if not force and not config_mod.sound_enabled():
            return False
        target = (
            resolve_target(token) if token is not None else _resolve_effective(category)
        )
        if target is None and force and token is None:
            target = resolve_target(config_mod.DEFAULT_SOUND_MAP.get(category, ""))
        if target is None:
            return False
        volume = config_mod.sound_volume()
        kind, value = target
        if kind == "synth":
            path = _sound_path(value)
            return _play_path(str(path), volume) if path is not None else False
        if kind == "file":
            return _play_path(value, volume) if Path(value).is_file() else False
        # system alias — winsound only (no volume control there)
        import winsound

        winsound.PlaySound(
            value, winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        )
        return True
    except Exception:
        logger.debug("play sound failed (category=%s)", category, exc_info=True)
        return False

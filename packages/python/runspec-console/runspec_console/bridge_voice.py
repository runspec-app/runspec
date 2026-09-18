"""bridge_voice.py — VoiceMixin: the in-app dictation seam.

The composer's mic button calls ``start_dictation`` / ``stop_dictation``; the
engine (``dictation.DictationSession`` over Moonshine Voice, the ``[voice]``
extra) streams partial / final text back as ``runspec:dictation_*`` events via
the Bridge's ``_dispatch`` queue, and the UI writes the finals into the
``<textarea>``. ``dictation_status`` is the availability probe the composer
reads on mount to render the button enabled / disabled-with-reason.

One session at a time: starting a new one stops the previous. The session is
seeded with **keyterms** — the fleet's runnable and host names — so the decoder
spells ``cert-check`` / ``db-3`` the way the catalogue does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import config as config_mod
from . import dictation
from .bridge_const import logger

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class VoiceMixin:
    _dictation: dictation.DictationSession | None

    # ── JS API ──

    def dictation_status(self) -> dict[str, Any]:
        """Availability + live state for the composer's mic button. ``available``
        false ⇒ ``reason`` explains (extra not installed, PortAudio missing,
        ``[voice] enabled = false``, bad model name)."""
        cfg = config_mod.read_voice()
        ok, reason = dictation.available(cfg)
        session = self._dictation
        model = ""
        try:
            model = dictation.normalise_model(cfg.get("model"))
        except ValueError:
            pass
        return {
            "available": ok,
            "reason": reason,
            "listening": bool(session is not None and session.active),
            "session_id": session.session_id
            if session is not None and session.active
            else None,
            "model": model,
            "language": cfg.get("language", "en"),
            "vendored": bool(cfg.get("model_dir")),
        }

    def start_dictation(self) -> dict[str, Any]:
        """Open the microphone and start streaming text into the composer.
        Returns ``{ok, session_id}`` immediately — the model opens on a worker
        thread (``runspec:dictation_state`` reports ``loading`` → ``listening``,
        or ``ended`` with a reason)."""
        cfg = config_mod.read_voice()
        ok, reason = dictation.available(cfg)
        if not ok:
            return {"ok": False, "error": reason}
        previous = self._dictation
        if previous is not None and previous.active:
            previous.stop()
        session = dictation.DictationSession(
            self._dispatch,  # type: ignore[attr-defined]
            config=cfg,
            keyterms=self._dictation_keyterms(),
        )
        self._dictation = session
        session.start()
        logger.info("dictation: session %s started", session.session_id)
        return {"ok": True, "session_id": session.session_id}

    def stop_dictation(self) -> dict[str, Any]:
        """Stop the current dictation session (idempotent). Queued audio is still
        finalised and delivered before the ``ended`` event."""
        session = self._dictation
        if session is None or not session.active:
            return {"ok": True, "session_id": None}
        session.stop()
        logger.info("dictation: session %s stop requested", session.session_id)
        return {"ok": True, "session_id": session.session_id}

    # ── internals ──

    def _dictation_keyterms(self) -> list[str]:
        """Runnable + host names from the discovery cache, as decoder-bias terms."""
        try:
            with self._lock:  # type: ignore[attr-defined]
                runnables = list(self._runnables_cache)  # type: ignore[attr-defined]
        except Exception:
            runnables = []
        names = [str(r.get("name") or "") for r in runnables]
        hosts = [str(r.get("host") or "") for r in runnables]
        return dictation.build_keyterms(names, hosts)

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
from .bridge_const import AI_OFF_MESSAGE, logger

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class VoiceMixin:
    _dictation: dictation.DictationSession | None
    # Dictation fills the chat prompt; with ``[console] ai = false`` there is no
    # assistant to dictate to, so the feature is refused (not degraded) like the
    # other agent surfaces — the UI hides it, and these methods refuse too so a
    # stale bundle or a direct JS call can't open the microphone.
    _ai_enabled: bool
    # The last "unavailable" reason we logged — a hidden mic button gives the
    # operator nothing to go on, so the reason goes to runspec-console.log once
    # per distinct reason (the status is polled on every composer mount).
    _dictation_reason_logged: str = ""

    # ── JS API ──

    def dictation_status(self) -> dict[str, Any]:
        """Availability + live state for the composer's mic button. ``available``
        false ⇒ ``reason`` explains (extra not installed, PortAudio missing,
        ``[voice] enabled = false``, bad model name)."""
        cfg = config_mod.read_voice()
        ok, reason = self._voice_available(cfg)
        if not ok and reason != getattr(self, "_dictation_reason_logged", ""):
            self._dictation_reason_logged = reason
            logger.info("dictation unavailable: %s", reason)
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
            "silence_timeout": float(cfg.get("silence_timeout", 0) or 0),
            "keep_loaded": bool(cfg.get("keep_loaded", True)),
            "model_loaded": dictation.model_loaded(),
        }

    def get_voice_config(self) -> dict[str, Any]:
        """The ``[voice]`` section for Settings → Voice, plus live availability
        (``available``/``reason``), whether the engine is resident
        (``model_loaded``) and the selectable model names."""
        cfg = config_mod.read_voice()
        ok, reason = self._voice_available(cfg)
        return {
            **cfg,
            "model": dictation.normalise_model(cfg.get("model"))
            if ok or not cfg.get("model")
            else cfg.get("model"),
            "available": ok,
            "reason": reason,
            "model_loaded": dictation.model_loaded(),
            "models": list(dictation.STREAMING_MODELS),
        }

    def save_voice_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate + persist the ``[voice]`` section (atomic, other sections
        untouched). A model/language/vendored-dir change — or turning
        ``keep_loaded`` off — releases the resident engine so the next press
        loads the right one; a live session is stopped first. Raises
        ``ValueError`` (→ rejected JS promise) on bad input."""
        if not isinstance(payload, dict):
            raise ValueError("voice config must be an object")
        section = _validate_voice(payload)
        before = config_mod.read_voice()

        def _mutate(cfg: dict[str, Any]) -> None:
            cfg["voice"] = section

        config_mod.update_config(_mutate)
        after = config_mod.read_voice()
        key_changed = dictation.model_cache_key(before) != dictation.model_cache_key(
            after
        )
        if key_changed or not after["keep_loaded"]:
            session = self._dictation
            if session is not None and session.active:
                session.stop()
            dictation.release_model()
        logger.info(
            "voice config saved (%s)",
            ", ".join(f"{k}={v!r}" for k, v in section.items()),
        )
        return self.get_voice_config()

    def list_voice_devices(self) -> list[dict[str, Any]]:
        """Input devices for the Settings → Voice microphone picker."""
        return dictation.list_input_devices()

    def start_dictation(self) -> dict[str, Any]:
        """Open the microphone and start streaming text into the composer.
        Returns ``{ok, session_id}`` immediately — the model opens on a worker
        thread (``runspec:dictation_state`` reports ``loading`` → ``listening``,
        or ``ended`` with a reason)."""
        cfg = config_mod.read_voice()
        ok, reason = self._voice_available(cfg)
        if not ok:
            return {"ok": False, "error": reason}
        previous = self._dictation
        if previous is not None and previous.active:
            previous.stop()
        session = dictation.DictationSession(
            self._dispatch,  # type: ignore[attr-defined]
            config=cfg,
            keyterms=self._dictation_keyterms(),
            silence_timeout=float(cfg.get("silence_timeout", 0) or 0),
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

    def _voice_available(self, cfg: dict[str, Any]) -> tuple[bool, str]:
        """``dictation.available`` behind the AI off-switch — neutral wording,
        never naming the flag (see docs/console-ai-off.md)."""
        if not getattr(self, "_ai_enabled", True):
            return (False, AI_OFF_MESSAGE)
        return dictation.available(cfg)

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


def _validate_voice(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce the Settings → Voice form into the ``[voice]`` table shape
    ``config.read_voice`` reads. Blank ``model_dir`` / ``device`` are omitted
    (= defaults) rather than written as empty strings."""
    out: dict[str, Any] = {"enabled": bool(payload.get("enabled", True))}
    language = str(payload.get("language") or "en").strip()
    if not language:
        raise ValueError("language must not be blank")
    out["language"] = language
    out["model"] = dictation.normalise_model(payload.get("model"))  # ValueError on junk
    model_dir = str(payload.get("model_dir") or "").strip()
    if model_dir:
        out["model_dir"] = model_dir
    device = payload.get("device")
    if isinstance(device, bool):
        raise ValueError("device must be an index or a name")
    if isinstance(device, (int, float)) and not isinstance(device, bool):
        out["device"] = int(device)
    elif isinstance(device, str) and device.strip():
        out["device"] = device.strip()
    try:
        interval = float(payload.get("update_interval", 0.5))
        silence = float(payload.get("silence_timeout", 8))
    except (TypeError, ValueError) as exc:
        raise ValueError("update_interval and silence_timeout must be numbers") from exc
    if not 0.1 <= interval <= 5:
        raise ValueError("update_interval must be between 0.1 and 5 seconds")
    if not 0 <= silence <= 300:
        raise ValueError(
            "silence_timeout must be between 0 and 300 seconds (0 = never)"
        )
    out["update_interval"] = interval
    out["silence_timeout"] = silence
    out["keep_loaded"] = bool(payload.get("keep_loaded", True))
    return out

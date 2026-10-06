"""
dictation.py — in-app voice dictation into the chat composer (the ``[voice]``
extra).

Speech is transcribed **on-device** by `Moonshine Voice
<https://github.com/moonshine-ai/moonshine>`_ (``pip install
runspec-console[voice]``), a streaming engine built for laptop CPUs. Recognition
runs **host-side in Python**: Moonshine's ``MicTranscriber`` captures the system
default microphone itself (via ``sounddevice``, whose wheel bundles PortAudio),
so the web view never touches ``getUserMedia`` and no audio crosses the JS↔Python
bridge — only small text events flow back through the Bridge's ``_dispatch``
queue:

- ``runspec:dictation_state``   ``{session_id, state, reason?, progress?, model?}``
  where ``state`` is ``loading`` (model opening / first-run download),
  ``listening`` (mic open) or ``ended`` (``reason``: ``stopped`` by the operator,
  ``silence`` — nothing recognised for ``[voice] silence_timeout`` seconds — or
  an error message).
- ``runspec:dictation_partial`` ``{session_id, text}`` — the in-progress text of
  the line being spoken, replaced as the recogniser refines it.
- ``runspec:dictation_final``   ``{session_id, text}`` — a finished line; the UI
  appends it into the composer's ``<textarea>``.

Dictation is an **input method, not a control surface**: the transcript lands in
the prompt box and the operator still reads it and presses Enter, so it sits
entirely below the autonomy / confirm gates that govern what the agent does.
Nothing auto-sends.

**Model distribution.** By default Moonshine fetches its model from
``download.moonshine.ai`` on first use into its own cache (``MOONSHINE_VOICE_CACHE``
overrides the directory). A fleet that blocks outbound model downloads sets
``[voice] model_dir`` to a **vendored** model directory (shipped through the
internal index / a package) and the engine never downloads anything.

The engine is wrapped behind an injectable factory so the session logic —
threading, event shaping, stop-during-load, keyterm biasing — is unit-tested
with no Moonshine, no microphone and no model. ``available()`` reports why the
feature is off (extra not installed, PortAudio missing, disabled by config) so
the composer can show a disabled mic with a tooltip instead of failing.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable, Iterable

logger = logging.getLogger("runspec.console.dictation")

Emit = Callable[[str, dict[str, Any]], None]

EVENT_STATE = "runspec:dictation_state"
EVENT_PARTIAL = "runspec:dictation_partial"
EVENT_FINAL = "runspec:dictation_final"

# Moonshine's streaming architectures, by the name used in ``[voice] model``.
# The catalog's recommended English model is medium-streaming (~245M params),
# which is what an unset ``model`` resolves to. Non-streaming arches (``tiny`` /
# ``base``) are deliberately not offered — dictation needs partial results.
STREAMING_MODELS: tuple[str, ...] = (
    "tiny-streaming",
    "base-streaming",
    "small-streaming",
    "medium-streaming",
)
DEFAULT_MODEL = "medium-streaming"

# Upper bound on decoder-bias terms handed to the engine — Moonshine's own
# guidance is to keep the list modest (a long list costs accuracy elsewhere).
MAX_KEYTERMS = 200


# ── pure helpers (unit-tested, no Moonshine) ──────────────────────────────────


def normalise_model(name: str | None) -> str:
    """Resolve ``[voice] model`` to a streaming architecture name.

    Empty/None ⇒ :data:`DEFAULT_MODEL`. A bare size (``"small"``) is accepted as
    its streaming variant so the config reads naturally. Unknown ⇒ ValueError
    (surfaced as the disabled-state reason rather than a crash)."""
    raw = (name or "").strip().lower()
    if not raw:
        return DEFAULT_MODEL
    if raw in STREAMING_MODELS:
        return raw
    if f"{raw}-streaming" in STREAMING_MODELS:
        return f"{raw}-streaming"
    raise ValueError(
        f"unknown [voice] model {name!r} — expected one of {', '.join(STREAMING_MODELS)}"
    )


def clean_keyterms(terms: Iterable[Any], *, limit: int = MAX_KEYTERMS) -> list[str]:
    """Dedupe/trim a list of decoder-bias terms.

    Moonshine separates terms with commas internally, so a term containing one
    is dropped (never split — a half term biases toward the wrong word). Order is
    preserved so callers can put the most important vocabulary first; the list is
    capped at ``limit``."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in terms:
        term = str(raw or "").strip()
        if not term or "," in term:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= limit:
            break
    return out


def build_keyterms(
    runnable_names: Iterable[str], host_names: Iterable[str]
) -> list[str]:
    """The console's vocabulary an operator is likely to say — runnable names
    (``cert-check``, ``upgrade-packages``) and fleet host names (``db-3``) — so
    the decoder spells them the way the catalogue does. Hosts first: they are
    the shorter, more ambiguous tokens."""
    return clean_keyterms([*host_names, *runnable_names])


def available(cfg: dict[str, Any]) -> tuple[bool, str]:
    """Whether dictation can run here: ``[voice] enabled``, the ``[voice]`` extra
    (``moonshine_voice``) importable, and microphone capture (``sounddevice`` —
    its import raises ``OSError`` when PortAudio is absent) loadable. Never
    raises; the reason is what the composer's tooltip shows."""
    if not cfg.get("enabled", True):
        return (False, "Dictation is disabled ([voice] enabled = false)")
    try:
        normalise_model(cfg.get("model"))
    except ValueError as exc:
        return (False, str(exc))
    try:
        import moonshine_voice  # noqa: F401
    except Exception:
        return (
            False,
            "moonshine-voice not installed — pip install runspec-console[voice]",
        )
    try:
        import sounddevice  # noqa: F401
    except Exception as exc:
        return (False, f"microphone capture unavailable: {exc}")
    return (True, "")


# ── warm model cache (pure; the engine object is opaque) ──────────────────────


class ModelCache:
    """Holds one loaded speech engine between dictation sessions so the second
    mic press is instant (``[voice] keep_loaded``). Keyed by what selects a
    model — language, architecture, vendored dir — so a config change replaces
    (and closes) the resident engine instead of reusing the wrong one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple[Any, ...] | None = None
        self._engine: Any = None

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    @property
    def key(self) -> tuple[Any, ...] | None:
        return self._key

    def get(self, key: tuple[Any, ...]) -> Any:
        with self._lock:
            return (
                self._engine if self._engine is not None and self._key == key else None
            )

    def put(self, key: tuple[Any, ...], engine: Any) -> None:
        with self._lock:
            old = self._engine if self._key != key else None
            self._key, self._engine = key, engine
        _close_quietly(old)

    def clear(self) -> None:
        with self._lock:
            old, self._key, self._engine = self._engine, None, None
        _close_quietly(old)


def _close_quietly(engine: Any) -> None:
    if engine is None:
        return
    try:
        engine.close()
    except Exception:
        logger.debug("dictation: engine.close failed", exc_info=True)


_MODEL_CACHE = ModelCache()


def model_cache_key(cfg: dict[str, Any]) -> tuple[Any, ...]:
    """What selects a model: (language, streaming arch, vendored dir)."""
    return (
        str(cfg.get("language") or "en").strip().lower(),
        normalise_model(cfg.get("model")),
        str(cfg.get("model_dir") or "").strip(),
    )


def acquire_engine(
    cfg: dict[str, Any],
    on_progress: Callable[[float, str], None],
    *,
    cache: ModelCache,
    load: Callable[[dict[str, Any], Callable[[float, str], None]], Any],
) -> tuple[Any, bool]:
    """Return ``(engine, reused)`` — the cached engine when its key matches the
    config, else a freshly ``load``-ed one that replaces whatever was cached."""
    key = model_cache_key(cfg)
    engine = cache.get(key)
    if engine is not None:
        return engine, True
    engine = load(cfg, on_progress)
    cache.put(key, engine)
    return engine, False


def release_model() -> None:
    """Drop the resident engine (config changed / ``keep_loaded`` turned off)."""
    _MODEL_CACHE.clear()


def model_loaded() -> bool:
    return _MODEL_CACHE.loaded


# ── engine factory (the only Moonshine-touching code) ─────────────────────────

# One engine load at a time: Moonshine's native-library singleton is not
# thread-safe (a second caller can see it half-built), and two loads would
# only race to fill the same cache slot anyway.
_LOAD_LOCK = threading.Lock()


def ensure_native_library(lib_cls: Any = None) -> None:
    """Load Moonshine's native library, surfacing the real reason if it fails.

    ``moonshine_voice``'s ``_MoonshineLib`` stores its singleton *before*
    loading the DLL / ``.so``, so when the load fails once (a missing VC++
    runtime, a clashing ``onnxruntime.dll``…) every later call gets an
    instance whose ``lib`` is ``None`` and dies with ``'NoneType' object has
    no attribute 'moonshine_get_stt_catalog'`` — the original error is gone.
    Drop a half-built instance, retry the load, and drop it again on failure
    so the next mic press retries instead of inheriting the broken state."""
    if lib_cls is None:
        from moonshine_voice.moonshine_api import _MoonshineLib as lib_cls

    inst = getattr(lib_cls, "_instance", None)
    if inst is not None and getattr(inst, "_lib", None) is not None:
        return
    lib_cls._instance = None
    lib_cls._lib = None  # class default; an instance attribute shadows it
    try:
        inst = lib_cls()
    except Exception as exc:
        lib_cls._instance = None
        raise RuntimeError(
            f"Moonshine's native speech library failed to load: {exc}"
        ) from exc
    if getattr(inst, "_lib", None) is None:
        lib_cls._instance = None
        raise RuntimeError("Moonshine's native speech library failed to load")


def _load_transcriber(
    cfg: dict[str, Any], on_progress: Callable[[float, str], None]
) -> Any:
    """Open a Moonshine ``Transcriber`` the way ``MicTranscriber.load`` would —
    catalogue download (or the vendored dir) plus the best-effort spelling
    model — but owned by *us*, so it can outlive one mic session."""
    from moonshine_voice import ModelArch, Transcriber, get_model_for_language
    from moonshine_voice.download import get_spelling_model_path

    language = str(cfg.get("language") or "en")
    arch = ModelArch[normalise_model(cfg.get("model")).upper().replace("-", "_")]
    options: dict[str, Any] = {}
    model_dir = cfg.get("model_dir")
    if model_dir:
        path = str(model_dir)
    else:
        path, arch = get_model_for_language(language, arch, on_progress=on_progress)
        try:
            spelling = get_spelling_model_path(language)
            if spelling:
                options["spelling_model_path"] = spelling
        except Exception:
            logger.debug("dictation: no spelling model for %s", language)
    return Transcriber(path, arch, options=options or None)


def _build_mic(cfg: dict[str, Any], on_progress: Callable[[float, str], None]) -> Any:
    """Configure a Moonshine ``MicTranscriber`` from ``[voice]`` — imported here
    so the module imports cleanly without the extra."""
    from moonshine_voice import ModelArch
    from moonshine_voice.mic_transcriber import MicTranscriber

    with _LOAD_LOCK:
        ensure_native_library()
        return _configure_mic(cfg, on_progress, ModelArch, MicTranscriber)


def _configure_mic(
    cfg: dict[str, Any],
    on_progress: Callable[[float, str], None],
    ModelArch: Any,
    MicTranscriber: Any,
) -> Any:
    mic = (
        MicTranscriber()
        .update_interval(float(cfg.get("update_interval") or 0.5))
        .on_progress(on_progress)
    )
    if cfg.get("keep_loaded", True):
        # Warm path: reuse the resident engine (instant), or load and keep it.
        engine, _reused = acquire_engine(
            cfg, on_progress, cache=_MODEL_CACHE, load=_load_transcriber
        )
        mic.use_transcriber(engine)
    else:
        _MODEL_CACHE.clear()  # free a previously warm engine
        model = normalise_model(cfg.get("model"))
        mic.language(str(cfg.get("language") or "en")).model_arch(
            ModelArch[model.upper().replace("-", "_")]
        )
        model_dir = cfg.get("model_dir")
        if model_dir:
            # Vendored model: load from disk, never download (the fleet case).
            mic.models_from(str(model_dir))
    device = cfg.get("device")
    if device not in (None, ""):
        mic.device(device)
    return mic


def list_input_devices() -> list[dict[str, Any]]:
    """Microphones PortAudio can open here — ``[{index, name, default}]`` for
    the Settings → Voice device picker. Empty when capture is unavailable."""
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        try:
            default_in = int(sd.default.device[0])
        except Exception:
            default_in = -1
        out: list[dict[str, Any]] = []
        for index, dev in enumerate(devices):
            if int(dev.get("max_input_channels", 0) or 0) <= 0:
                continue
            out.append(
                {
                    "index": index,
                    "name": str(dev.get("name") or f"device {index}"),
                    "default": index == default_in,
                }
            )
        return out
    except Exception:
        return []


# ── session ───────────────────────────────────────────────────────────────────


class DictationSession:
    """One microphone → composer dictation session.

    ``start()`` returns immediately; a worker thread opens the model (blocking —
    the first run may download it), starts the mic and then relays Moonshine's
    partial / final line callbacks as bridge events. ``stop()`` also returns
    immediately: Moonshine's ``stop()`` joins its transcription worker so any
    queued audio is still finalised (and delivered as a last ``final`` event)
    before the ``ended`` state is emitted.

    ``transcriber_factory`` builds the engine object (anything with Moonshine's
    ``MicTranscriber`` surface: ``on_text``/``on_line``/``on_error``/``load``/
    ``start``/``stop``/``close``/``set_keyterms``); tests inject a fake."""

    def __init__(
        self,
        emit: Emit,
        *,
        config: dict[str, Any],
        keyterms: Iterable[str] = (),
        transcriber_factory: Callable[..., Any] | None = None,
        silence_timeout: float = 0.0,
    ) -> None:
        self.session_id = uuid.uuid4().hex[:12]
        # Auto-stop after this many seconds with no recognised speech (0 = never).
        # "Activity" is any partial/final with text; the clock starts when the
        # mic opens, so the operator gets the full window to begin speaking.
        self._silence_timeout = max(0.0, float(silence_timeout or 0.0))
        self._last_activity = time.monotonic()
        self._emit = emit
        self._config = dict(config)
        self._keyterms = clean_keyterms(keyterms)
        self._factory = transcriber_factory or (
            lambda on_progress: _build_mic(self._config, on_progress)
        )
        self._lock = threading.Lock()
        self._mic: Any = None
        self._stopped = False
        self._ended = False
        self._last_progress = -1.0
        self.state = "idle"

    # ── public ──

    @property
    def active(self) -> bool:
        return not self._ended and not self._stopped

    def start(self) -> None:
        threading.Thread(
            target=self._run, name="runspec-dictation", daemon=True
        ).start()

    def stop(self) -> None:
        """Request stop. Idempotent; the ``ended`` event follows once the engine
        has flushed (or immediately if it never started)."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            mic = self._mic
            self._mic = None
        if mic is None:
            # Never reached `listening` (still loading, or already ended): the
            # worker thread notices `_stopped` after load and cleans up itself —
            # unless it already finished, in which case end now.
            if self.state in ("idle", "ended"):
                self._end("stopped")
            return
        threading.Thread(
            target=self._shutdown, args=(mic, "stopped"), daemon=True
        ).start()

    # ── worker ──

    def _run(self) -> None:
        self._set_state("loading")
        try:
            mic = self._factory(self._on_progress)
            mic.on_text(self._on_partial).on_line(self._on_final).on_error(
                self._on_error
            )
            mic.load()
            if self._keyterms:
                try:
                    mic.set_keyterms(self._keyterms)
                except Exception:
                    # Biasing is a nicety (and only streaming arches support it);
                    # dictation works without it.
                    logger.debug("dictation: set_keyterms failed", exc_info=True)
            with self._lock:
                if self._stopped:
                    stopped_early = True
                else:
                    stopped_early = False
                    self._mic = mic
            if stopped_early:
                self._shutdown(mic, "stopped")
                return
            mic.start()
            self._touch()
            self._set_state(
                "listening", model=normalise_model(self._config.get("model"))
            )
            if self._silence_timeout > 0:
                threading.Thread(
                    target=self._watch_silence,
                    name="runspec-dictation-silence",
                    daemon=True,
                ).start()
        except Exception as exc:
            logger.warning("dictation: failed to start: %s", exc)
            with self._lock:
                self._mic = None
            self._end(f"could not start dictation: {exc}")

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    def _watch_silence(self) -> None:
        """End the session once nothing has been recognised for
        ``silence_timeout`` seconds — so a mic left open in a quiet room turns
        itself off instead of listening until the operator remembers."""
        tick = min(0.5, max(0.05, self._silence_timeout / 4))
        while True:
            time.sleep(tick)
            with self._lock:
                if self._stopped or self._ended:
                    return
                idle = time.monotonic() - self._last_activity
                if idle < self._silence_timeout:
                    continue
                self._stopped = True
                mic = self._mic
                self._mic = None
            if mic is not None:
                self._shutdown(mic, "silence")
            else:
                self._end("silence")
            return

    def _shutdown(self, mic: Any, reason: str) -> None:
        try:
            mic.stop()
        except Exception:
            logger.debug("dictation: mic.stop failed", exc_info=True)
        try:
            mic.close()
        except Exception:
            logger.debug("dictation: mic.close failed", exc_info=True)
        self._end(reason)

    # ── engine callbacks (Moonshine worker thread) ──

    def _on_progress(self, fraction: float, filename: str) -> None:
        # Model download/open progress — only on first use (or a vendored model
        # opening). Emit at 5% steps so a download doesn't flood the UI queue.
        try:
            frac = float(fraction)
        except Exception:
            return
        if frac - self._last_progress < 0.05 and frac < 1.0:
            return
        self._last_progress = frac
        self._set_state("loading", progress=round(frac, 2), file=str(filename or ""))

    def _on_partial(self, text: str) -> None:
        if self._ended:
            return
        if (text or "").strip():
            self._touch()
        self._emit(EVENT_PARTIAL, {"session_id": self.session_id, "text": text or ""})

    def _on_final(self, line: Any) -> None:
        if self._ended:
            return
        text = str(getattr(line, "text", line) or "").strip()
        if not text:
            return
        self._touch()
        self._emit(EVENT_FINAL, {"session_id": self.session_id, "text": text})

    def _on_error(self, exc: BaseException) -> None:
        logger.warning("dictation: engine error: %s", exc)
        with self._lock:
            mic = self._mic
            self._mic = None
            self._stopped = True
        if mic is not None:
            threading.Thread(
                target=self._shutdown, args=(mic, f"error: {exc}"), daemon=True
            ).start()
        else:
            self._end(f"error: {exc}")

    # ── events ──

    def _set_state(self, state: str, **extra: Any) -> None:
        self.state = state
        self._emit(
            EVENT_STATE, {"session_id": self.session_id, "state": state, **extra}
        )

    def _end(self, reason: str) -> None:
        with self._lock:
            if self._ended:
                return
            self._ended = True
        self._set_state("ended", reason=reason)

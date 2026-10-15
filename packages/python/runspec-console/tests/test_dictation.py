"""Tests for dictation.py (the in-app mic → composer session) and the
VoiceMixin bridge surface — no Moonshine, no microphone, no model: the engine
is a fake injected through ``transcriber_factory``."""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

import pytest

from runspec_console import dictation
from runspec_console.dictation import (
    EVENT_FINAL,
    EVENT_PARTIAL,
    EVENT_STATE,
    DictationSession,
    build_keyterms,
    clean_keyterms,
    normalise_model,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def wait_for(pred, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


class FakeLine:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeMic:
    """Mirrors the MicTranscriber surface DictationSession drives."""

    def __init__(self, *, block_load: threading.Event | None = None, fail_load=None):
        self.calls: list[str] = []
        self.keyterms: list[str] | None = None
        self._text_cb = None
        self._line_cb = None
        self._error_cb = None
        self._block_load = block_load
        self._fail_load = fail_load

    # chainable config
    def on_text(self, cb):
        self._text_cb = cb
        return self

    def on_line(self, cb):
        self._line_cb = cb
        return self

    def on_error(self, cb):
        self._error_cb = cb
        return self

    def load(self):
        self.calls.append("load")
        if self._block_load is not None:
            self._block_load.wait(3)
        if self._fail_load is not None:
            raise self._fail_load
        return self

    def set_keyterms(self, terms):
        self.keyterms = list(terms)

    def start(self):
        self.calls.append("start")
        return self

    def stop(self):
        self.calls.append("stop")

    def close(self):
        self.calls.append("close")

    # test drivers
    def say_partial(self, text: str) -> None:
        self._text_cb(text)

    def say_final(self, text: str) -> None:
        self._line_cb(FakeLine(text))

    def fail(self, exc: BaseException) -> None:
        self._error_cb(exc)


class Emitted:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def __call__(self, event: str, detail: dict[str, Any]) -> None:
        with self._lock:
            self.events.append((event, dict(detail)))

    def states(self) -> list[str]:
        return [d["state"] for e, d in self.events if e == EVENT_STATE]

    def last_state(self) -> dict[str, Any]:
        return [d for e, d in self.events if e == EVENT_STATE][-1]

    def texts(self, event: str) -> list[str]:
        return [d["text"] for e, d in self.events if e == event]


def make_session(mic: FakeMic, emit: Emitted, **kw: Any) -> DictationSession:
    return DictationSession(
        emit,
        config={"model": "", "language": "en"},
        transcriber_factory=lambda on_progress: mic,
        **kw,
    )


# ── pure helpers ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, "medium-streaming"),
        ("", "medium-streaming"),
        ("  ", "medium-streaming"),
        ("small", "small-streaming"),
        ("Tiny-Streaming", "tiny-streaming"),
        ("base-streaming", "base-streaming"),
    ],
)
def test_normalise_model(raw, expected):
    assert normalise_model(raw) == expected


def test_normalise_model_rejects_unknown_and_non_streaming():
    with pytest.raises(ValueError, match="unknown \\[voice\\] model"):
        normalise_model("large")
    # A bare size is shorthand for its *streaming* variant — the non-streaming
    # Moonshine arches (which can't give partials) are never selected.
    assert normalise_model("tiny") == "tiny-streaming"
    with pytest.raises(ValueError):
        normalise_model("base-large")


def test_clean_keyterms_dedupes_trims_and_drops_commas():
    terms = clean_keyterms(
        [" cert-check ", "cert-check", "CERT-CHECK", "", None, "a,b", "db-3"]
    )
    assert terms == ["cert-check", "db-3"]


def test_clean_keyterms_caps_at_limit():
    assert len(clean_keyterms((f"t{i}" for i in range(500)), limit=7)) == 7


def test_build_keyterms_puts_hosts_first():
    assert build_keyterms(["which", "cert-check"], ["db-3", "web-01", "db-3"]) == [
        "db-3",
        "web-01",
        "which",
        "cert-check",
    ]


def test_available_disabled_by_config():
    ok, reason = dictation.available({"enabled": False})
    assert not ok and "[voice] enabled" in reason


def test_available_bad_model_name():
    ok, reason = dictation.available({"enabled": True, "model": "huge"})
    assert not ok and "unknown [voice] model" in reason


def test_available_reports_missing_extra(monkeypatch):
    # A None entry in sys.modules makes `import moonshine_voice` raise ImportError.
    monkeypatch.setitem(sys.modules, "moonshine_voice", None)
    ok, reason = dictation.available({"enabled": True, "model": ""})
    assert not ok and "runspec-console[voice]" in reason


def test_available_reports_missing_portaudio(monkeypatch):
    import types

    monkeypatch.setitem(
        sys.modules, "moonshine_voice", types.ModuleType("moonshine_voice")
    )
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    ok, reason = dictation.available({"enabled": True, "model": ""})
    assert not ok and "microphone capture unavailable" in reason


# ── session lifecycle ─────────────────────────────────────────────────────────


def test_session_streams_partials_and_finals_then_stops():
    mic, emit = FakeMic(), Emitted()
    s = make_session(mic, emit, keyterms=["db-3", "cert-check"])
    assert s.state == "idle"
    s.start()
    wait_for(lambda: s.state == "listening")
    assert emit.states() == ["loading", "listening"]
    assert emit.last_state()["model"] == "medium-streaming"
    assert mic.calls == ["load", "start"]
    assert mic.keyterms == ["db-3", "cert-check"]
    assert s.active

    mic.say_partial("check the over")
    mic.say_partial("check the overnight batch")
    mic.say_final("  check the overnight batch on db-3 ")
    mic.say_final("   ")  # blank finals are dropped
    assert emit.texts(EVENT_PARTIAL) == ["check the over", "check the overnight batch"]
    assert emit.texts(EVENT_FINAL) == ["check the overnight batch on db-3"]
    sid = emit.events[0][1]["session_id"]
    assert all(d["session_id"] == sid for _, d in emit.events)

    s.stop()
    wait_for(lambda: s.state == "ended")
    assert mic.calls == ["load", "start", "stop", "close"]
    assert emit.last_state() == {
        "session_id": sid,
        "state": "ended",
        "reason": "stopped",
    }
    assert not s.active
    # Late callbacks after the end are ignored.
    mic.say_final("ghost")
    assert emit.texts(EVENT_FINAL) == ["check the overnight batch on db-3"]
    # Idempotent stop.
    s.stop()
    assert emit.states().count("ended") == 1


def test_stop_during_model_load_never_opens_the_mic():
    gate = threading.Event()
    mic, emit = FakeMic(block_load=gate), Emitted()
    s = make_session(mic, emit)
    s.start()
    wait_for(lambda: "load" in mic.calls)
    s.stop()  # while load() is blocked
    gate.set()
    wait_for(lambda: s.state == "ended")
    assert "start" not in mic.calls
    assert mic.calls == ["load", "stop", "close"]
    assert emit.states() == ["loading", "ended"]


def test_factory_failure_ends_with_reason():
    emit = Emitted()

    def boom(on_progress):
        raise RuntimeError("PortAudio device -1")

    s = DictationSession(emit, config={"model": ""}, transcriber_factory=boom)
    s.start()
    wait_for(lambda: s.state == "ended")
    assert (
        emit.last_state()["reason"] == "could not start dictation: PortAudio device -1"
    )
    assert not s.active


def test_load_failure_ends_with_reason():
    mic, emit = FakeMic(fail_load=OSError("model dir missing")), Emitted()
    s = make_session(mic, emit)
    s.start()
    wait_for(lambda: s.state == "ended")
    assert "model dir missing" in emit.last_state()["reason"]


def test_keyterm_failure_is_not_fatal():
    class NoBias(FakeMic):
        def set_keyterms(self, terms):
            raise RuntimeError("not a streaming architecture")

    mic, emit = NoBias(), Emitted()
    s = make_session(mic, emit, keyterms=["db-3"])
    s.start()
    wait_for(lambda: s.state == "listening")
    assert "start" in mic.calls


def test_engine_error_ends_session():
    mic, emit = FakeMic(), Emitted()
    s = make_session(mic, emit)
    s.start()
    wait_for(lambda: s.state == "listening")
    mic.fail(RuntimeError("stream overflow"))
    wait_for(lambda: s.state == "ended")
    assert emit.last_state()["reason"] == "error: stream overflow"
    assert mic.calls == ["load", "start", "stop", "close"]


def test_progress_is_throttled_to_five_percent_steps():
    emit = Emitted()
    captured: dict[str, Any] = {}

    def factory(on_progress):
        captured["progress"] = on_progress
        return FakeMic()

    s = DictationSession(emit, config={"model": ""}, transcriber_factory=factory)
    s.start()
    wait_for(lambda: s.state == "listening")
    cb = captured["progress"]
    for frac in (0.0, 0.01, 0.02, 0.05, 0.06, 0.5, 1.0):
        cb(frac, "model.bin")
    progress = [
        d["progress"] for e, d in emit.events if e == EVENT_STATE and "progress" in d
    ]
    assert progress == [0.0, 0.05, 0.5, 1.0]


def test_stop_before_start_ends_immediately():
    mic, emit = FakeMic(), Emitted()
    s = make_session(mic, emit)
    s.stop()
    assert s.state == "ended"
    assert emit.states() == ["ended"]
    assert mic.calls == []


# ── silence auto-stop ─────────────────────────────────────────────────────────


def test_silence_timeout_ends_session_after_no_speech():
    mic, emit = FakeMic(), Emitted()
    s = make_session(mic, emit, silence_timeout=0.3)
    s.start()
    wait_for(lambda: s.state == "listening")
    wait_for(lambda: s.state == "ended", timeout=3)
    assert emit.last_state()["reason"] == "silence"
    assert mic.calls == ["load", "start", "stop", "close"]


def test_speech_keeps_resetting_the_silence_clock():
    mic, emit = FakeMic(), Emitted()
    s = make_session(mic, emit, silence_timeout=0.3)
    s.start()
    wait_for(lambda: s.state == "listening")
    # Keep talking for longer than the timeout — partials with text reset it.
    for _ in range(6):
        time.sleep(0.1)
        mic.say_partial("still talking")
        assert s.state == "listening"
    # Whitespace-only partials are not activity.
    for _ in range(5):
        time.sleep(0.1)
        mic.say_partial("  ")
    wait_for(lambda: s.state == "ended", timeout=3)
    assert emit.last_state()["reason"] == "silence"


def test_silence_timeout_zero_never_auto_stops():
    mic, emit = FakeMic(), Emitted()
    s = make_session(mic, emit, silence_timeout=0)
    s.start()
    wait_for(lambda: s.state == "listening")
    time.sleep(0.4)
    assert s.state == "listening"
    s.stop()
    wait_for(lambda: s.state == "ended")
    assert emit.last_state()["reason"] == "stopped"


def test_operator_stop_wins_over_silence_watchdog():
    mic, emit = FakeMic(), Emitted()
    s = make_session(mic, emit, silence_timeout=0.3)
    s.start()
    wait_for(lambda: s.state == "listening")
    s.stop()
    wait_for(lambda: s.state == "ended")
    time.sleep(0.5)
    assert emit.states().count("ended") == 1
    assert emit.last_state()["reason"] == "stopped"


# ── bridge mixin ──────────────────────────────────────────────────────────────


class _VoiceBridge:
    from runspec_console.bridge_voice import VoiceMixin as _M

    dictation_status = _M.dictation_status
    start_dictation = _M.start_dictation
    stop_dictation = _M.stop_dictation
    get_voice_config = _M.get_voice_config
    save_voice_config = _M.save_voice_config
    list_voice_devices = _M.list_voice_devices
    _dictation_keyterms = _M._dictation_keyterms

    _voice_available = _M._voice_available

    def __init__(self, runnables, *, ai_enabled=True):
        self._dictation = None
        self._ai_enabled = ai_enabled
        self._lock = threading.Lock()
        self._runnables_cache = runnables
        self.dispatched: list[tuple[str, dict[str, Any]]] = []

    def _dispatch(self, event, detail):
        self.dispatched.append((event, detail))


@pytest.fixture
def voice_bridge(monkeypatch):
    from runspec_console import bridge_voice

    monkeypatch.setattr(
        bridge_voice.config_mod,
        "read_voice",
        lambda: {
            "enabled": True,
            "model": "small",
            "language": "en",
            "model_dir": None,
        },
    )
    monkeypatch.setattr(bridge_voice.dictation, "available", lambda cfg: (True, ""))
    return _VoiceBridge(
        [
            {"name": "cert-check", "host": "web-01"},
            {"name": "which", "host": "db-3"},
            {"name": "which", "host": "web-01"},
        ]
    )


def test_bridge_status_reports_model_and_keyterms(voice_bridge):
    st = voice_bridge.dictation_status()
    assert st == {
        "available": True,
        "reason": "",
        "listening": False,
        "session_id": None,
        "model": "small-streaming",
        "language": "en",
        "vendored": False,
        "silence_timeout": 0.0,
        "keep_loaded": True,
        "model_loaded": False,
    }
    assert voice_bridge._dictation_keyterms() == [
        "web-01",
        "db-3",
        "cert-check",
        "which",
    ]


def test_bridge_start_refuses_when_unavailable(voice_bridge, monkeypatch):
    from runspec_console import bridge_voice

    monkeypatch.setattr(
        bridge_voice.dictation, "available", lambda cfg: (False, "no mic here")
    )
    assert voice_bridge.start_dictation() == {"ok": False, "error": "no mic here"}
    assert voice_bridge.dictation_status()["available"] is False


def test_bridge_start_stop_round_trip(voice_bridge, monkeypatch):
    from runspec_console import bridge_voice

    created: list[dict[str, Any]] = []

    class FakeSession:
        def __init__(self, emit, *, config, keyterms, silence_timeout=0.0):
            created.append(
                {
                    "config": config,
                    "keyterms": list(keyterms),
                    "silence_timeout": silence_timeout,
                }
            )
            self.session_id = f"s{len(created)}"
            self.active = True
            self.stopped = False

        def start(self):
            pass

        def stop(self):
            self.stopped = True
            self.active = False

    monkeypatch.setattr(bridge_voice.dictation, "DictationSession", FakeSession)
    assert voice_bridge.stop_dictation() == {"ok": True, "session_id": None}
    r = voice_bridge.start_dictation()
    assert r == {"ok": True, "session_id": "s1"}
    assert created[0]["keyterms"] == ["web-01", "db-3", "cert-check", "which"]
    assert created[0]["config"]["model"] == "small"
    assert created[0]["silence_timeout"] == 0.0
    assert voice_bridge.dictation_status()["listening"] is True
    assert voice_bridge.dictation_status()["session_id"] == "s1"
    first = voice_bridge._dictation
    # Starting again stops the previous session.
    assert voice_bridge.start_dictation() == {"ok": True, "session_id": "s2"}
    assert first.stopped
    assert voice_bridge.stop_dictation() == {"ok": True, "session_id": "s2"}
    assert voice_bridge.dictation_status()["listening"] is False
    assert voice_bridge.stop_dictation() == {"ok": True, "session_id": None}


def test_bridge_status_logs_unavailable_reason_once(voice_bridge, monkeypatch, caplog):
    import logging

    from runspec_console import bridge_voice

    monkeypatch.setattr(
        bridge_voice.dictation, "available", lambda cfg: (False, "no engine")
    )
    with caplog.at_level(logging.INFO, logger=bridge_voice.logger.name):
        voice_bridge.dictation_status()
        voice_bridge.dictation_status()
    assert [r.getMessage() for r in caplog.records] == [
        "dictation unavailable: no engine"
    ]


# ── warm model cache ──────────────────────────────────────────────────────────


class FakeEngine:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_acquire_engine_reuses_cached_engine_for_same_config():
    cache = dictation.ModelCache()
    loads: list[str] = []

    def load(cfg, on_progress):
        loads.append(cfg["model"])
        return FakeEngine(cfg["model"])

    cfg = {"language": "en", "model": "small", "model_dir": None}
    e1, reused1 = dictation.acquire_engine(cfg, lambda *a: None, cache=cache, load=load)
    e2, reused2 = dictation.acquire_engine(cfg, lambda *a: None, cache=cache, load=load)
    assert (reused1, reused2) == (False, True)
    assert e1 is e2 and loads == ["small"] and cache.loaded


def test_acquire_engine_replaces_and_closes_on_config_change():
    cache = dictation.ModelCache()
    load = lambda cfg, on_progress: FakeEngine(cfg["model"])  # noqa: E731
    e1, _ = dictation.acquire_engine(
        {"language": "en", "model": "small"}, lambda *a: None, cache=cache, load=load
    )
    e2, reused = dictation.acquire_engine(
        {"language": "en", "model": "medium"}, lambda *a: None, cache=cache, load=load
    )
    assert reused is False and e2 is not e1 and e1.closed and not e2.closed
    # Language / vendored dir are part of the key too.
    assert dictation.model_cache_key(
        {"language": "EN ", "model": "", "model_dir": ""}
    ) == (
        "en",
        "medium-streaming",
        "",
    )
    assert dictation.model_cache_key(
        {"language": "en", "model": "", "model_dir": "C:/m"}
    ) != (dictation.model_cache_key({"language": "en", "model": ""}))
    cache.clear()
    assert e2.closed and not cache.loaded


def test_module_cache_release(monkeypatch):
    cache = dictation.ModelCache()
    monkeypatch.setattr(dictation, "_MODEL_CACHE", cache)
    eng = FakeEngine("x")
    cache.put(("en", "small-streaming", ""), eng)
    assert dictation.model_loaded()
    dictation.release_model()
    assert eng.closed and not dictation.model_loaded()


def test_list_input_devices_without_portaudio(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    assert dictation.list_input_devices() == []


# ── Settings → Voice get/save ─────────────────────────────────────────────────


def test_save_voice_config_writes_section_and_releases_on_model_change(
    voice_bridge, monkeypatch
):
    from runspec_console import bridge_voice

    stored: dict[str, Any] = {}
    reads = [
        {
            "enabled": True,
            "model": "small",
            "language": "en",
            "model_dir": None,
            "keep_loaded": True,
            "silence_timeout": 8.0,
            "update_interval": 0.5,
            "device": None,
        },
    ]

    def read_voice():
        return dict(reads[-1])

    def update_config(mutate):
        cfg: dict[str, Any] = {"other": {"keep": 1}}
        mutate(cfg)
        stored.update(cfg)
        v = cfg["voice"]
        reads.append(
            {
                "enabled": v["enabled"],
                "model": v["model"],
                "language": v["language"],
                "model_dir": v.get("model_dir"),
                "keep_loaded": v["keep_loaded"],
                "silence_timeout": v["silence_timeout"],
                "update_interval": v["update_interval"],
                "device": v.get("device"),
            }
        )
        return cfg

    released: list[bool] = []
    monkeypatch.setattr(bridge_voice.config_mod, "read_voice", read_voice)
    monkeypatch.setattr(bridge_voice.config_mod, "update_config", update_config)
    monkeypatch.setattr(
        bridge_voice.dictation, "release_model", lambda: released.append(True)
    )
    monkeypatch.setattr(bridge_voice.dictation, "model_loaded", lambda: True)

    got = voice_bridge.get_voice_config()
    assert got["model"] == "small-streaming" and got["models"][0] == "tiny-streaming"
    assert got["available"] is True and got["model_loaded"] is True

    # Same model → no release.
    out = voice_bridge.save_voice_config(
        {
            "enabled": True,
            "language": "en",
            "model": "small",
            "model_dir": "",
            "device": "",
            "update_interval": 0.5,
            "silence_timeout": 12,
            "keep_loaded": True,
        }
    )
    assert stored["voice"] == {
        "enabled": True,
        "language": "en",
        "model": "small-streaming",
        "update_interval": 0.5,
        "silence_timeout": 12.0,
        "keep_loaded": True,
    }
    assert stored["other"] == {"keep": 1}
    assert out["silence_timeout"] == 12.0 and released == []

    # Model change → release; device index + vendored dir written.
    voice_bridge.save_voice_config(
        {
            "enabled": True,
            "language": "en",
            "model": "medium-streaming",
            "model_dir": " C:/models/en ",
            "device": 3,
            "update_interval": 1,
            "silence_timeout": 0,
            "keep_loaded": True,
        }
    )
    assert (
        stored["voice"]["model_dir"] == "C:/models/en"
        and stored["voice"]["device"] == 3
    )
    assert released == [True]

    # keep_loaded off → release too.
    voice_bridge.save_voice_config(
        {
            "enabled": False,
            "language": "en",
            "model": "medium-streaming",
            "device": "USB Mic",
            "update_interval": 1,
            "silence_timeout": 0,
            "keep_loaded": False,
        }
    )
    assert (
        stored["voice"]["device"] == "USB Mic" and stored["voice"]["enabled"] is False
    )
    assert released == [True, True]


@pytest.mark.parametrize(
    "bad, msg",
    [
        ({"model": "huge"}, "unknown \\[voice\\] model"),
        ({"language": "  "}, "language must not be blank"),
        ({"update_interval": 9}, "update_interval"),
        ({"silence_timeout": -1}, "silence_timeout"),
        ({"update_interval": "abc"}, "must be numbers"),
        ({"device": True}, "device"),
    ],
)
def test_save_voice_config_rejects_bad_input(voice_bridge, monkeypatch, bad, msg):
    from runspec_console import bridge_voice

    monkeypatch.setattr(
        bridge_voice.config_mod,
        "update_config",
        lambda m: pytest.fail("must not write"),
    )
    with pytest.raises(ValueError, match=msg):
        voice_bridge.save_voice_config({"model": "small", "language": "en", **bad})


def test_save_voice_config_rejects_non_object(voice_bridge):
    with pytest.raises(ValueError):
        voice_bridge.save_voice_config("nope")  # type: ignore[arg-type]


def test_voice_refused_when_ai_off(monkeypatch):
    from runspec_console import bridge_voice
    from runspec_console.bridge_const import AI_OFF_MESSAGE

    monkeypatch.setattr(
        bridge_voice.config_mod,
        "read_voice",
        lambda: {"enabled": True, "model": "", "language": "en", "model_dir": None},
    )
    # Even with the engine present, AI off wins — and the message is neutral.
    monkeypatch.setattr(bridge_voice.dictation, "available", lambda cfg: (True, ""))
    monkeypatch.setattr(
        bridge_voice.dictation,
        "DictationSession",
        lambda *a, **k: pytest.fail("must not start a session"),
    )
    b = _VoiceBridge([], ai_enabled=False)
    st = b.dictation_status()
    assert st["available"] is False and st["reason"] == AI_OFF_MESSAGE
    assert b.start_dictation() == {"ok": False, "error": AI_OFF_MESSAGE}
    assert b.get_voice_config()["available"] is False
    assert b.stop_dictation() == {"ok": True, "session_id": None}


# ── native-library singleton recovery ──


def _fake_moonshine_lib(outcomes: list[Any]) -> type:
    """Mimics ``moonshine_voice``'s ``_MoonshineLib``: the singleton is stored
    *before* the load, so a failed load leaves an instance with ``_lib = None``.
    Each construction pops the next outcome (an exception, or a lib object)."""

    class FakeLib:
        _instance = None
        _lib = None
        loads = 0

        def __new__(cls):
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._load_library()
            return cls._instance

        def _load_library(self) -> None:
            type(self).loads += 1
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            self._lib = outcome

    return FakeLib


def test_ensure_native_library_recovers_a_poisoned_singleton():
    lib = _fake_moonshine_lib([OSError("VCRUNTIME140_1.dll missing"), "lib"])
    with pytest.raises(OSError):
        lib()  # moonshine's own first call fails…
    assert lib._instance is not None and lib._instance._lib is None  # …and sticks
    dictation.ensure_native_library(lib)
    assert lib._instance._lib == "lib"
    assert lib.loads == 2


def test_ensure_native_library_surfaces_the_real_error_and_retries_next_time():
    lib = _fake_moonshine_lib([OSError("VCRUNTIME140_1.dll missing"), "lib"])
    with pytest.raises(RuntimeError, match="VCRUNTIME140_1.dll missing"):
        dictation.ensure_native_library(lib)
    assert lib._instance is None  # not left half-built
    dictation.ensure_native_library(lib)
    assert lib._instance._lib == "lib"


def test_ensure_native_library_is_a_no_op_once_loaded():
    lib = _fake_moonshine_lib(["lib"])
    dictation.ensure_native_library(lib)
    dictation.ensure_native_library(lib)
    assert lib.loads == 1


# ── Windows C++ runtime diagnosis + start-up preload ──


def test_runtime_hint_names_an_old_winrt_runtime():
    hint = dictation.runtime_hint(
        lambda: (r"C:\venv\Lib\site-packages\winrt\msvcp140.dll", (14, 29, 30157, 0))
    )
    assert "14.29.30157.0" in hint and "14.40" in hint
    assert "notifications extra" in hint and "restart" in hint


def test_runtime_hint_suggests_the_redistributable_for_other_old_runtimes():
    hint = dictation.runtime_hint(
        lambda: (r"C:\Windows\System32\msvcp140.dll", (14, 28, 29913, 0))
    )
    assert "Visual C++ Redistributable" in hint


@pytest.mark.parametrize(
    "found",
    [None, (r"C:\Windows\System32\msvcp140.dll", (14, 44, 35211, 0))],
)
def test_runtime_hint_silent_when_runtime_absent_or_new_enough(found):
    assert dictation.runtime_hint(lambda: found) == ""


def test_load_failure_carries_the_runtime_hint(monkeypatch):
    monkeypatch.setattr(dictation, "runtime_hint", lambda: "old runtime loaded")
    lib = _fake_moonshine_lib([OSError("DLL initialization routine failed")])
    with pytest.raises(RuntimeError, match="initialization routine failed — old"):
        dictation.ensure_native_library(lib)


def test_preload_is_a_no_op_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    called = []
    monkeypatch.setattr(dictation, "ensure_native_library", lambda: called.append(1))
    dictation.preload_native_library()
    assert called == []


def test_preload_on_windows_loads_and_never_raises(monkeypatch, caplog):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: object())

    def boom() -> None:
        raise RuntimeError("Moonshine's native speech library failed to load: x")

    monkeypatch.setattr(dictation, "ensure_native_library", boom)
    dictation.preload_native_library()  # logged, not raised
    assert "failed to load" in caplog.text

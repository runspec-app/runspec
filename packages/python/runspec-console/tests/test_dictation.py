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


# ── bridge mixin ──────────────────────────────────────────────────────────────


class _VoiceBridge:
    from runspec_console.bridge_voice import VoiceMixin as _M

    dictation_status = _M.dictation_status
    start_dictation = _M.start_dictation
    stop_dictation = _M.stop_dictation
    _dictation_keyterms = _M._dictation_keyterms

    def __init__(self, runnables):
        self._dictation = None
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
        def __init__(self, emit, *, config, keyterms):
            created.append({"config": config, "keyterms": list(keyterms)})
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

"""
test_disclaimer.py — the first-use AI-use disclaimer popup.

Covers:
  1. config.read_disclaimer() defaults + validation (enabled requires a body;
     hard-off is left to the bridge, which folds in the AI switch).
  2. disclaimer_hash change-detection (text and version).
  3. read/write of the machine-local acknowledgement file.
  4. Bridge._compute_disclaimer_state / disclaimer() / acknowledge_disclaimer():
     the tri-state, re-prompt on wording change, hard-off when AI is off, the
     acknowledgement write + audit line, and the telemetry provider shape.
  5. eventlog.write_application_event: off-Windows no-op + the eventcreate argv.

No network anywhere; the OTel enrichment test is skipped when the SDK is absent.
"""

from __future__ import annotations

import json
import threading

import pytest

import runspec_console.config as cfg
from runspec_console import eventlog
from runspec_console import observability as obs
from runspec_console.bridge_config_ssh import ConfigSshMixin
from runspec_console.bridge_reporting import ReportingMixin


def _cfg_path(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(cfg, "app_dir", lambda: tmp_path)
    monkeypatch.setattr("runspec_console.config.app_dir", lambda: tmp_path)


# ── 1. read_disclaimer ────────────────────────────────────────────────────────


def test_read_disclaimer_default_disabled(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)
    d = cfg.read_disclaimer()
    assert d == {
        "enabled": False,
        "title": "Notice",
        "text": "",
        "acknowledge_label": "I acknowledge",
        "version": "",
        "always_show": False,
    }


def test_read_disclaimer_enabled_requires_text(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)
    # enabled=true but no body ⇒ a no-op, so enabled resolves False.
    cfg.write_config({"console": {"disclaimer": {"enabled": True}}})
    assert cfg.read_disclaimer()["enabled"] is False
    # With a body it stays enabled and carries the wording + custom fields.
    cfg.write_config(
        {
            "console": {
                "disclaimer": {
                    "enabled": True,
                    "title": "  AI Use  ",
                    "text": "  Use responsibly.  ",
                    "acknowledge_label": "  I agree  ",
                    "version": " 2 ",
                }
            }
        }
    )
    d = cfg.read_disclaimer()
    assert d["enabled"] is True
    assert d["title"] == "AI Use"
    assert d["text"] == "Use responsibly."
    assert d["acknowledge_label"] == "I agree"
    assert d["version"] == "2"


# ── 2. disclaimer_hash ────────────────────────────────────────────────────────


def test_disclaimer_hash_changes_with_text_and_version():
    base = cfg.disclaimer_hash("hello", "")
    assert base == cfg.disclaimer_hash("hello", "")  # stable
    assert base != cfg.disclaimer_hash("hello!", "")  # text change
    assert base != cfg.disclaimer_hash("hello", "2")  # version change
    assert len(base) == 32


# ── 3. ack file round-trip ────────────────────────────────────────────────────


def test_ack_roundtrip(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)
    assert cfg.read_disclaimer_ack() == {}  # absent ⇒ empty
    cfg.write_disclaimer_ack({"hash": "abc", "version": "1", "acknowledged_at": "t"})
    assert cfg.read_disclaimer_ack()["hash"] == "abc"


def test_ack_unparseable_reads_empty(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)
    cfg.disclaimer_ack_path().write_text(
        "this is not = valid = toml [", encoding="utf-8"
    )
    assert cfg.read_disclaimer_ack() == {}  # safe direction: re-prompt


# ── 4. Bridge methods (lightweight harness) ───────────────────────────────────


def _mk_bridge(ai=True, user="Tester"):
    """A minimal object carrying just the disclaimer/audit methods + the shared
    attributes they touch (mirrors test_config_theme's _brand_theme harness)."""

    class _B(ConfigSshMixin, ReportingMixin):
        _ai_enabled = ai
        _audit_lock = threading.Lock()

        def _current_user(self):
            return user

    return _B()


def _enable(monkeypatch, tmp_path, *, text="Be careful.", version=""):
    _cfg_path(monkeypatch, tmp_path)
    cfg.write_config(
        {"console": {"disclaimer": {"enabled": True, "text": text, "version": version}}}
    )


def test_state_not_required_when_disabled(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)  # no disclaimer configured
    st = _mk_bridge().disclaimer()
    assert st["enabled"] is False and st["needs_ack"] is False


def test_state_pending_then_acknowledged(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))  # audit log → tmp
    b = _mk_bridge()
    assert b.disclaimer()["needs_ack"] is True
    assert b._compute_disclaimer_state()["state"] == "pending"

    res = b.acknowledge_disclaimer()
    assert res == {"ok": True, "needs_ack": False}
    assert b.disclaimer()["needs_ack"] is False
    assert b._compute_disclaimer_state()["state"] == "acknowledged"


def test_reprompt_when_wording_changes(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, text="Original wording.")
    monkeypatch.setenv("HOME", str(tmp_path))
    b = _mk_bridge()
    b.acknowledge_disclaimer()
    assert b.disclaimer()["needs_ack"] is False
    # The company revises the wording — the acknowledgement no longer matches.
    cfg.write_config(
        {"console": {"disclaimer": {"enabled": True, "text": "Revised wording."}}}
    )
    assert b.disclaimer()["needs_ack"] is True
    assert b._compute_disclaimer_state()["state"] == "pending"


def test_reprompt_when_version_bumped(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, text="Same text.", version="1")
    monkeypatch.setenv("HOME", str(tmp_path))
    b = _mk_bridge()
    b.acknowledge_disclaimer()
    assert b.disclaimer()["needs_ack"] is False
    cfg.write_config(
        {
            "console": {
                "disclaimer": {"enabled": True, "text": "Same text.", "version": "2"}
            }
        }
    )
    assert b.disclaimer()["needs_ack"] is True


def test_always_show_reprompts_after_acknowledgement(monkeypatch, tmp_path):
    # always_show re-shows the notice every launch even once acknowledged.
    _cfg_path(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg.write_config(
        {
            "console": {
                "disclaimer": {
                    "enabled": True,
                    "text": "Standing reminder.",
                    "always_show": True,
                }
            }
        }
    )
    b = _mk_bridge()
    assert b.disclaimer()["needs_ack"] is True
    # Acknowledging dismisses it for the session (needs_ack False in the response,
    # which the frontend uses to close the modal) and still records the ack…
    res = b.acknowledge_disclaimer()
    assert res == {"ok": True, "needs_ack": False}
    assert cfg.read_disclaimer_ack().get("hash")  # the ack was still written
    # …but a fresh state read (i.e. the next launch) re-shows it despite the ack.
    assert b.disclaimer()["needs_ack"] is True
    assert b._compute_disclaimer_state()["state"] == "pending"


def test_hard_off_when_ai_disabled(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    b = _mk_bridge(ai=False)
    d = b.disclaimer()
    assert d["enabled"] is False and d["needs_ack"] is False
    assert b._compute_disclaimer_state()["state"] == "not_required"
    # Acknowledging a disabled disclaimer is a harmless no-op.
    assert b.acknowledge_disclaimer() == {"ok": True, "needs_ack": False}


def test_disclaimer_frontend_shape(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, text="Body.", version="1")
    cfg.write_config(
        {
            "console": {
                "disclaimer": {
                    "enabled": True,
                    "title": "AI Notice",
                    "text": "Body.",
                    "acknowledge_label": "Got it",
                    "version": "1",
                }
            }
        }
    )
    d = _mk_bridge().disclaimer()
    assert d["title"] == "AI Notice"
    assert d["text"] == "Body."
    assert d["acknowledge_label"] == "Got it"
    assert d["version"] == "1"


def test_acknowledge_writes_audit_line(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, text="Careful.", version="7")
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads USERPROFILE on Windows, not HOME — set both so the audit
    # log lands in tmp_path/logs on every platform.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _mk_bridge(user="Alice").acknowledge_disclaimer()
    log = tmp_path / "logs" / ReportingMixin._TOOL_AUDIT_FILE
    assert log.exists()
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["extra"]["event"] == "disclaimer_ack"
    assert rec["extra"]["user"] == "Alice"
    assert rec["extra"]["disclaimer_version"] == "7"
    assert rec["extra"]["disclaimer_hash"]


# ── telemetry provider ────────────────────────────────────────────────────────


def test_disclaimer_telemetry_none_when_disabled(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)
    assert _mk_bridge().disclaimer() and _mk_bridge()._disclaimer_telemetry() is None


def test_disclaimer_telemetry_states(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, text="X.", version="3")
    monkeypatch.setenv("HOME", str(tmp_path))
    b = _mk_bridge()
    pending = b._disclaimer_telemetry()
    assert pending == {"state": "pending"}  # no timestamp/version until acknowledged
    b.acknowledge_disclaimer()
    acked = b._disclaimer_telemetry()
    assert acked["state"] == "acknowledged"
    assert acked["version"] == "3"
    assert acked["acknowledged_at"]  # ISO timestamp present


# ── 5. eventlog ───────────────────────────────────────────────────────────────


def test_eventlog_noop_off_windows(monkeypatch):
    monkeypatch.setattr(eventlog, "is_windows", lambda: False)
    called = {"n": 0}
    monkeypatch.setattr(
        eventlog.subprocess, "run", lambda *a, **k: called.__setitem__("n", 1)
    )
    assert eventlog.write_application_event(source="x", message="y") is False
    assert called["n"] == 0  # never shelled out


def test_eventlog_builds_eventcreate_argv(monkeypatch):
    monkeypatch.setattr(eventlog, "is_windows", lambda: True)
    captured = {}

    class _Res:
        returncode = 0

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Res()

    monkeypatch.setattr(eventlog.subprocess, "run", _fake_run)
    ok = eventlog.write_application_event(source="mycorp-console", message="ack by A")
    assert ok is True
    argv = captured["argv"]
    assert argv[0] == "eventcreate"
    assert "/L" in argv and argv[argv.index("/L") + 1] == "APPLICATION"
    assert argv[argv.index("/SO") + 1] == "mycorp-console"
    assert argv[argv.index("/ID") + 1] == str(eventlog.DISCLAIMER_EVENT_ID)
    assert argv[argv.index("/D") + 1] == "ack by A"


# ── OTel enrichment (skipped without the SDK) ─────────────────────────────────

_OK, _ = obs.available()
otel_only = pytest.mark.skipif(not _OK, reason="opentelemetry SDK not installed")


@otel_only
def test_span_and_metric_carry_disclaimer_state(monkeypatch):
    """When a provider is registered, a turn span carries the full state (incl.
    timestamp) and the turn metric carries only the bounded tri-state label."""
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    try:
        obs.set_disclaimer_provider(
            lambda: {
                "state": "acknowledged",
                "acknowledged_at": "2026-01-01T00:00:00Z",
                "version": "9",
            }
        )
        obs.init_tracing_for_test(exporter, capture_content=False)
        obs.init_metrics_for_test(reader)

        handle = obs.begin_turn("chat", "c1", model="m")
        obs.end_turn(handle)
        obs.record_turn(kind="chat", duration_ms=1)

        span = exporter.get_finished_spans()[0]
        assert span.attributes["runspec.disclaimer.state"] == "acknowledged"
        assert (
            span.attributes["runspec.disclaimer.acknowledged_at"]
            == "2026-01-01T00:00:00Z"
        )
        assert span.attributes["runspec.disclaimer.version"] == "9"

        data = reader.get_metrics_data()
        labels = [
            dict(dp.attributes)
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for metric in sm.metrics
            if metric.name == "runspec.agent.turns"
            for dp in metric.data.data_points
        ]
        assert labels and labels[0]["runspec.disclaimer.state"] == "acknowledged"
        # The unbounded timestamp must never become a metric label.
        assert "runspec.disclaimer.acknowledged_at" not in labels[0]
    finally:
        obs.set_disclaimer_provider(None)
        obs.reset_for_test()
        obs.reset_metrics_for_test()

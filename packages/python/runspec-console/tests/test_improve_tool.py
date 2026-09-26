"""
test_improve_tool.py — the ``run_improve_report`` built-in agent tool + the Bridge
glue for Improve (schema, autonomy, dispatch, persistence, mailto).

Mirrors the light-stub pattern used across the suite (bind the methods under test
off the real ``Bridge`` class). The pure analysis lives in ``improve.py`` (see
``test_improve.py``); here we exercise the wiring: the tool is a read-only,
deferred built-in; it routes through ``_run_tool_sync``; config gates it; and the
report round-trips through the machine-local store with a mailto to the configured
developers.
"""

from __future__ import annotations

import json

from runspec_console import config as config_mod
from runspec_console.bridge import Bridge
from runspec_console.bridge_const import (
    _BUILTIN_TOOL_NAMES,
    _HOT_BUILTIN_NAMES,
    _IMPROVE_TOOL_NAMES,
)


class _Stub:
    _improve_tools = Bridge._improve_tools
    _run_improve_tool = Bridge._run_improve_tool
    _effective_tool_autonomy = Bridge._effective_tool_autonomy
    run_improve_report = Bridge.run_improve_report
    list_improve_reports = Bridge.list_improve_reports
    load_improve_report = Bridge.load_improve_report
    delete_improve_report = Bridge.delete_improve_report
    improve_mailto = Bridge.improve_mailto
    synthesize_improve_report = Bridge.synthesize_improve_report
    _utility_adapter = Bridge._utility_adapter
    _build_improve_report = Bridge._build_improve_report
    _improve_catalog_rows = Bridge._improve_catalog_rows
    _gather_improve_conversations = Bridge._gather_improve_conversations
    _improve_product_name = Bridge._improve_product_name
    _improve_path = Bridge._improve_path
    _load_improve = Bridge._load_improve
    _save_improve = Bridge._save_improve

    def app_title(self):
        return "Acme Console"

    def get_history_all(self):
        return []

    _get_adapter_result = None

    def _get_adapter(self, system_override=None):
        return self._get_adapter_result


def _redirect_store(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "improve_reports_dir", lambda: tmp_path / "improve")
    monkeypatch.setattr(config_mod, "conversations_dir", lambda: tmp_path / "conv")
    (tmp_path / "conv").mkdir(parents=True, exist_ok=True)


# ── membership + autonomy ────────────────────────────────────────────────────
def test_improve_is_a_deferred_readonly_builtin():
    assert "run_improve_report" in _IMPROVE_TOOL_NAMES
    assert _IMPROVE_TOOL_NAMES <= _BUILTIN_TOOL_NAMES
    # NOT hot — heavy/rare, fine deferred behind describe_tool/use_tool in search mode.
    assert not (_IMPROVE_TOOL_NAMES & _HOT_BUILTIN_NAMES)


def test_improve_is_autonomous():
    assert _Stub()._effective_tool_autonomy("run_improve_report", {}) == "autonomous"


# ── schema + config gating ───────────────────────────────────────────────────
def test_schema_offered_when_enabled(monkeypatch):
    monkeypatch.setattr(config_mod, "read_improve", lambda: {"enabled": True})
    (tool,) = _Stub()._improve_tools()
    assert tool["name"] == "run_improve_report"
    assert "comment" in tool["input_schema"]["properties"]
    assert "window_days" in tool["input_schema"]["properties"]


def test_schema_dropped_when_disabled(monkeypatch):
    monkeypatch.setattr(config_mod, "read_improve", lambda: {"enabled": False})
    assert _Stub()._improve_tools() == []


# ── dispatch + execution ─────────────────────────────────────────────────────
def _cfg(**over):
    base = {
        "enabled": True,
        "product_name": "",
        "developer_emails": ["dev@x.com"],
        "window_days": 30,
        "max_conversations": 200,
        "max_incidents": 40,
        "llm_synthesis": True,
    }
    base.update(over)
    return base


def test_dispatch_routes_to_improve_handler(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "read_improve", lambda: _cfg())
    _redirect_store(monkeypatch, tmp_path)
    out = Bridge._run_tool_sync(_Stub(), "run_improve_report", {"window_days": 7})
    data = json.loads(out)
    assert data["kind"] == "evaluation"
    assert data["report_id"]
    assert "top_incidents" in data


def test_run_tool_refuses_when_disabled(monkeypatch):
    monkeypatch.setattr(config_mod, "read_improve", lambda: _cfg(enabled=False))
    out = _Stub()._run_improve_tool({})
    assert out.startswith("Error:")


def test_comment_makes_a_problem_report(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "read_improve", lambda: _cfg())
    _redirect_store(monkeypatch, tmp_path)
    out = _Stub()._run_improve_tool({"comment": "agent is slow"})
    assert json.loads(out)["kind"] == "problem_report"


# ── public JS API: persistence round-trip ────────────────────────────────────
def test_report_round_trips_through_store(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "read_improve", lambda: _cfg())
    _redirect_store(monkeypatch, tmp_path)
    stub = _Stub()
    rep = stub.run_improve_report(7, "")
    rid = rep["id"]
    # listed, loadable, and a sidecar markdown was written
    listed = stub.list_improve_reports()
    assert any(r["id"] == rid for r in listed)
    assert stub.load_improve_report(rid)["id"] == rid
    assert (tmp_path / "improve" / f"{rid}.md").exists()
    # product name falls back to app_title when [improve] product_name is blank
    assert rep["product_name"] == "Acme Console"
    # delete removes both files
    assert stub.delete_improve_report(rid)
    assert stub.load_improve_report(rid) is None


def test_mailto_builds_url_without_opening(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "read_improve", lambda: _cfg())
    _redirect_store(monkeypatch, tmp_path)
    opened = {}
    monkeypatch.setattr(
        "runspec_console.bridge_improve.webbrowser.open",
        lambda u: opened.setdefault("url", u),
    )
    stub = _Stub()
    rep = stub.run_improve_report(7, "")
    url = stub.improve_mailto(rep["id"])
    assert url.startswith("mailto:dev%40x.com?subject=")
    assert opened["url"] == url


def test_synthesis_without_provider_returns_note(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "read_improve", lambda: _cfg())
    _redirect_store(monkeypatch, tmp_path)
    stub = _Stub()  # _get_adapter returns None
    rep = stub.run_improve_report(7, "")
    out = stub.synthesize_improve_report(rep["id"])
    assert "synthesis_error" in out and "provider" in out["synthesis_error"]


def test_synthesis_with_fake_adapter_attaches_recommendations(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "read_improve", lambda: _cfg())
    _redirect_store(monkeypatch, tmp_path)

    class _Resp:
        text = '{"summary": "ok", "recommendations": [{"category": "runnable_args", "target": "drain", "why": "w", "fix": "f", "confidence": "high"}]}'

    class _Adapter:
        async def chat(self, messages, tools):
            return _Resp()

    stub = _Stub()
    stub._get_adapter_result = _Adapter()
    rep = stub.run_improve_report(7, "")
    out = stub.synthesize_improve_report(rep["id"])
    assert out["synthesis"]["summary"] == "ok"
    assert out["synthesis"]["recommendations"][0]["target"] == "drain"
    # markdown + learnings regenerated with the recommendations
    assert "Recommendations" in out["markdown"]
    assert 'category = "runnable_args"' in out["learnings_toml"]
    # persisted, so a reload sees the synthesis
    assert stub.load_improve_report(rep["id"])["synthesis"]["summary"] == "ok"


def test_load_improve_overlays_live_developer_emails(monkeypatch, tmp_path):
    # A report generated before [improve] developer_emails was set: the stored
    # recipients are empty. Adding the config afterward must take effect on the
    # existing report (the UI's email button + the mailto), so _load_improve
    # overlays the live config rather than trusting the frozen snapshot.
    _redirect_store(monkeypatch, tmp_path)
    d = tmp_path / "improve"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r1.json").write_text(
        json.dumps({"id": "r1", "developer_emails": []}), encoding="utf-8"
    )
    monkeypatch.setattr(
        config_mod,
        "read_improve",
        lambda: _cfg(developer_emails=["new@x.com", "lead@x.com"]),
    )
    rep = _Stub().load_improve_report("r1")
    assert rep is not None
    assert rep["developer_emails"] == ["new@x.com", "lead@x.com"]

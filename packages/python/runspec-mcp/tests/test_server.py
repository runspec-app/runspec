"""Server routing, manual refusal, and the tools/list_changed notification wiring."""

from __future__ import annotations

import runspec_mcp.catalog as catalog_mod
from runspec_mcp.config import FleetConfig
from runspec_mcp.server import FleetServer

_ITEMS = [
    {"runnable": "greeter", "config_autonomy": "confirm", "spec": {"autonomy": "autonomous", "args": {}}},
    {"runnable": "rotate", "config_autonomy": "confirm", "spec": {"autonomy": "manual", "args": {}}},
]


def _server(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog_mod, "_discover_items", lambda *a, **k: _ITEMS)
    monkeypatch.setenv("RUNSPEC_PLUGIN_MANIFEST", str(tmp_path / "autonomy.json"))
    cfg = FleetConfig(hosts=[{"name": "box", "runspec_paths": ["/v/env-a/bin/runspec"]}])
    srv = FleetServer(cfg, pool=None)
    srv._catalog.refresh()
    written: list[dict] = []
    monkeypatch.setattr(srv, "_write", lambda m: written.append(m))
    return srv, written


def test_initialize_advertises_listchanged(monkeypatch, tmp_path):
    srv, _ = _server(monkeypatch, tmp_path)
    resp = srv._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert resp["result"]["capabilities"] == {"tools": {"listChanged": True}}


def test_tools_list_includes_controls_and_runnables(monkeypatch, tmp_path):
    srv, _ = _server(monkeypatch, tmp_path)
    resp = srv._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert {"fleet_hosts", "fleet_refresh", "greeter", "rotate"} <= names


def test_manual_refused(monkeypatch, tmp_path):
    srv, _ = _server(monkeypatch, tmp_path)
    resp = srv._handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "rotate", "arguments": {}}})
    assert resp["result"]["isError"] is True
    assert "manual" in resp["result"]["content"][0]["text"]


def test_unknown_tool(monkeypatch, tmp_path):
    srv, _ = _server(monkeypatch, tmp_path)
    resp = srv._handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}})
    assert resp["result"]["isError"] is True


def test_notification_suppressed_until_ready(monkeypatch, tmp_path):
    srv, written = _server(monkeypatch, tmp_path)
    # Before serve loop: a catalog change must NOT push a notification.
    srv._on_catalog_change()
    assert written == []
    # After ready: it does.
    srv._notify_enabled = True
    srv._on_catalog_change()
    assert any(m.get("method") == "notifications/tools/list_changed" for m in written)


def test_fleet_refresh_control(monkeypatch, tmp_path):
    srv, _ = _server(monkeypatch, tmp_path)
    resp = srv._handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "fleet_refresh", "arguments": {}}})
    assert resp["result"]["isError"] is False
    assert "tool(s)" in resp["result"]["content"][0]["text"]

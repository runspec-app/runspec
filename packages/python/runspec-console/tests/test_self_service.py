"""
test_self_service.py — the self-service catalog registry (runspec_self_service.toml).

Covers the pure load/save/normalise helpers, the Config Sync wiring, and the
bridge CRUD seam (get_self_service / save_self_service) bound onto a bare stub.
This registry replaces the runspec.toml ``discoverable = ["self-service"]``
marking — self-service designation is a front-end concern, configured here.
"""

from __future__ import annotations

import threading
import types

from runspec_console import configmerge, gitsync
from runspec_console.bridge import Bridge
from runspec_console.self_service import (
    load_self_service,
    normalise_self_service,
    save_self_service,
    self_service_names,
)

# ── pure helpers ──────────────────────────────────────────────────────────────


def test_roundtrip_with_description(tmp_path):
    p = tmp_path / "runspec_self_service.toml"
    save_self_service(
        p,
        [
            {"name": "reset-password", "description": "Password resets"},
            {"name": "request-vm"},
        ],
    )
    rows = load_self_service(p)
    assert rows == [
        {"name": "reset-password", "description": "Password resets"},
        {"name": "request-vm"},
    ]
    assert self_service_names(rows) == {"reset-password", "request-vm"}


def test_load_absent_file(tmp_path):
    assert load_self_service(tmp_path / "nope.toml") == []


def test_normalise_dedupes_case_insensitively_and_drops_blanks():
    rows = normalise_self_service(
        [
            {"name": "reset-password"},
            {"name": "  "},
            {"name": "Reset-Password"},
            {"name": "request-vm"},
        ]
    )
    assert [e["name"] for e in rows] == ["reset-password", "request-vm"]


# ── Config Sync wiring ────────────────────────────────────────────────────────


def test_file_is_synced_and_merges_by_name():
    assert "runspec_self_service.toml" in gitsync.KNOWN_FILES
    assert configmerge.COLLECTION_FILES["runspec_self_service.toml"] == (
        "self_service",
        "name",
    )


# ── bridge CRUD (bare stub) ───────────────────────────────────────────────────


def _stub(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "runspec_console.config.self_service_path",
        lambda: tmp_path / "runspec_self_service.toml",
    )
    ns = types.SimpleNamespace()
    for name in ("get_self_service", "save_self_service"):
        setattr(ns, name, types.MethodType(getattr(Bridge, name), ns))
    ns._lock = threading.Lock()
    ns._published: list[tuple[str, dict]] = []
    ns._dispatch = lambda *a, **k: ns._published.append(a)
    ns._publish_catalogs = lambda: None
    return ns


def test_get_self_service_empty_by_default(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    assert ns.get_self_service() == []


def test_save_then_get_roundtrips_and_dispatches(tmp_path, monkeypatch):
    ns = _stub(tmp_path, monkeypatch)
    ns.save_self_service([{"name": "reset-password"}, {"name": "  "}])
    # Blank dropped, persisted, and read back normalised.
    assert [e["name"] for e in ns.get_self_service()] == ["reset-password"]
    # The UI is nudged to re-read.
    assert any(a[0] == "runspec:self_service_changed" for a in ns._published)

"""collect-diagnostics composition: probe selection + per-key error isolation."""

from __future__ import annotations

from runspec_windows import diagnostics


def test_collect_selected_probes(monkeypatch) -> None:
    monkeypatch.setitem(diagnostics._PROBES, "proxy", lambda _t: {"winhttp": {"direct": True}})
    monkeypatch.setitem(diagnostics._PROBES, "dns", lambda _t: {"interfaces": []})
    report = diagnostics.collect_diagnostics("host", include=["proxy", "dns"])
    assert report["target"] == "host"
    assert "collected_at" in report
    assert report["proxy"] == {"winhttp": {"direct": True}}
    assert report["dns"] == {"interfaces": []}
    assert "vpn" not in report  # not selected


def test_one_probe_failure_is_isolated(monkeypatch) -> None:
    def boom(_t: str) -> dict:
        raise RuntimeError("gpresult blocked by policy")

    monkeypatch.setitem(diagnostics._PROBES, "gpo", boom)
    monkeypatch.setitem(diagnostics._PROBES, "dns", lambda _t: {"interfaces": [{"interface": "Ethernet"}]})
    report = diagnostics.collect_diagnostics("host", include=["gpo", "dns"])
    assert report["gpo"] == {"error": "gpresult blocked by policy"}
    assert report["dns"]["interfaces"][0]["interface"] == "Ethernet"  # sibling still collected


def test_unknown_probe_reported(monkeypatch) -> None:
    report = diagnostics.collect_diagnostics("host", include=["nope"])
    assert report["nope"]["error"].startswith("unknown probe")

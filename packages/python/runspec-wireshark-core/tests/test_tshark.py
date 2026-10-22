"""Every helper against the real tshark and the sample capture (skipped when
tshark isn't installed). The sample's traffic is described in
scripts/make_sample_capture.py."""

from __future__ import annotations

import json
import os
import shutil

import pytest

import runspec_wireshark_core as ws
from runspec_wireshark_core import WiresharkConfig, WiresharkError

from .conftest import KEYS, needs_tshark

pytestmark = needs_tshark


def test_check(config) -> None:
    out = ws.check(config)
    assert out["tshark"] and out["version"].count(".") == 2


def test_list_interfaces_hides_extcap(config) -> None:
    out = ws.list_interfaces(config)
    assert all(row["kind"] == "network" for row in out["interfaces"])
    assert out["count"] + out["hidden"] == ws.list_interfaces(config, all=True)["count"]


def test_file_info(config, sample) -> None:
    out = ws.file_info(config, sample)
    assert out["packets"] == 96 and out["file_type"] == "pcapng" and out["start"].endswith("Z")


def test_file_info_without_capinfos(config, sample, monkeypatch) -> None:
    real = ws.find_tool

    def no_capinfos(name, cfg=None):
        if name == "capinfos":
            raise ws.ToolNotFoundError("no capinfos")
        return real(name, cfg)

    monkeypatch.setattr("runspec_wireshark_core.info.find_tool", no_capinfos)
    out = ws.file_info(config, sample)
    assert out["packets"] == 96 and out["data_bytes"] == 14482 and out["start"].endswith("Z")


def test_packets_page_and_streams(config, sample) -> None:
    out = ws.packets(config, sample, limit=5, offset=5)
    assert out["total"] == 96 and out["count"] == 5 and out["next_offset"] == 10
    assert [p["no"] for p in out["packets"]] == [6, 7, 8, 9, 10]
    syn = out["packets"][1]
    assert syn["src"] == "10.20.0.15:52829" and syn["dst"] == "10.20.1.80:8080" and syn["stream"] == "tcp 0"


def test_packets_filter_and_absolute_time(config, sample) -> None:
    out = ws.packets(config, sample, filter="tcp.flags.reset == 1", absolute_time=True)
    assert out["total"] == 4 and not out["truncated"]
    assert out["packets"][0]["time"].startswith("2026-10-22T13:08:40")


def test_packets_bad_filter(config, sample) -> None:
    with pytest.raises(WiresharkError, match="bad display filter"):
        ws.packets(config, sample, filter="tcp.port = = 80")


def test_packet_detail(config, sample) -> None:
    out = ws.packet(config, sample, 95)
    titles = [layer["title"] for layer in out["layers"]]
    assert titles[0].startswith("Frame 95") and titles[-1] == "Transport Layer Security"
    assert any("Unknown CA" in line for line in out["layers"][-1]["lines"])
    narrowed = ws.packet(config, sample, 95, layers=["tcp"], include=["bytes"])
    assert "lines" not in narrowed["layers"][2] and "[Stream index: 7]" in narrowed["layers"][3]["lines"]
    assert narrowed["bytes"][0].startswith("0000  02 00 0a 14")


def test_packet_raw_and_missing(config, sample) -> None:
    raw = ws.packet(config, sample, 62, raw=True)
    assert raw["frame"]["frame.number"] == "62" and "tls" in raw
    with pytest.raises(WiresharkError, match="no packet 9999"):
        ws.packet(config, sample, 9999)


def test_fields(config, sample) -> None:
    out = ws.fields(config, sample, ["dns.qry.name", "dns.a"], filter="dns.flags.response == 1")
    assert out["total"] == 3
    assert out["rows"][0] == {"no": 2, "dns.qry.name": "api.example.com", "dns.a": "10.20.1.43"}
    with pytest.raises(ValueError):
        ws.fields(config, sample, [])


def test_top(config, sample) -> None:
    out = ws.top(config, sample, "tcp.dstport")
    assert out["values"][0]["value"] == "8080" and {"8443", "53"} - {v["value"] for v in out["values"]} == {"53"} and out["missing"] == 6
    by_bytes = ws.top(config, sample, "ip.dst", by="bytes", limit=1)
    assert by_bytes["values"][0]["value"] == "10.20.0.15" and by_bytes["truncated"]


def test_protocols(config, sample) -> None:
    out = ws.protocols(config, sample)
    names = [p["protocol"] for p in out["protocols"]]
    assert out["frames"] == 96 and {"dns", "http", "tls"} <= set(names)
    shallow = ws.protocols(config, sample, max_depth=2)
    assert {p["protocol"] for p in shallow["protocols"]} == {"eth", "ip"}


def test_conversations(config, sample) -> None:
    tcp = ws.conversations(config, sample)
    assert tcp["total"] == 8
    by_stream = {c["stream"]: c for c in tcp["conversations"]}
    assert by_stream[4]["flags"] == ["SYN", "RST"] and by_stream[4]["b"] == "10.20.1.80:9999"
    assert by_stream[2]["a"] == "10.20.0.15:43807"
    ip = ws.conversations(config, sample, type="ip")
    assert ip["total"] == 3 and sum(c["packets"] for c in ip["conversations"]) == 96
    with pytest.raises(ValueError):
        ws.conversations(config, sample, type="sctp")


def test_endpoints(config, sample) -> None:
    out = ws.endpoints(config, sample)
    top = out["endpoints"][0]
    assert top["address"] == "10.20.0.15" and top["packets"] == 96
    assert top["tx"]["packets"] + top["rx"]["packets"] == 96


def test_expert(config, sample) -> None:
    out = ws.expert(config, sample)
    assert out["messages"] == [{"severity": "warning", "group": "Sequence", "message": "Connection reset (RST)", "count": 4, "first_packet": 58}]
    notes = ws.expert(config, sample, severity="note")
    assert notes["by_severity"]["note"] == 10


def test_io(config, sample) -> None:
    out = ws.io(config, sample, limit=10)
    assert out["interval"] == 0.1 and sum(b["packets"] for b in out["buckets"]) == 96
    fine = ws.io(config, sample, interval=0.01, limit=5)
    assert fine["truncated"] and fine["count"] == 5 and fine["empty_buckets"] > 0


def test_stats(config, sample) -> None:
    out = ws.stats(config, sample, "http,tree")
    assert any("500 Internal Server Error" in line for line in out["lines"])
    with pytest.raises(ValueError, match="credentials"):
        ws.stats(config, sample, "credentials")
    with pytest.raises(WiresharkError, match="Invalid -z argument"):
        ws.stats(config, sample, "bogus,thing")


def test_follow_tcp(config, sample) -> None:
    out = ws.follow(config, sample, stream=2)
    assert out["a"] == "10.20.0.15:43807" and out["b"] == "10.20.1.80:8080"
    assert out["chunks"][0]["text"].startswith("GET /api/orders?day=today HTTP/1.1\r\n")
    assert out["chunks"][-1]["text"] == '{"error":"database timeout"}' and not out["truncated"]
    cut = ws.follow(config, sample, stream=2, max_bytes=50)
    assert cut["truncated"] and len(cut["chunks"]) == 1 and len(cut["chunks"][0]["text"]) == 50


def test_follow_tls_needs_keys(config, sample) -> None:
    locked = ws.follow(config, sample, protocol="tls", stream=5)
    assert locked["chunks"] == [] and "--keylog-file" in locked["hint"]
    keyed = ws.follow(WiresharkConfig(keylog_file=str(KEYS)), sample, protocol="tls", stream=5)
    assert any('{"healthy":true}' in c["text"] for c in keyed["chunks"])


def test_dns(config, sample) -> None:
    out = ws.dns(config, sample)
    assert out["rcodes"] == {"NOERROR": 1, "NXDOMAIN": 2}
    assert out["lookups"][-1]["name"] == "api.example.com" and out["lookups"][-1]["answers"] == ["10.20.1.43"]
    failed = ws.dns(config, sample, failed=True)
    assert {r["name"] for r in failed["lookups"]} == {"portal.example.com", "nope.example.com"}


def test_http(config, sample) -> None:
    out = ws.http(config, sample)
    assert out["by_status"] == {"200": 1, "201": 1, "404": 1, "500": 1} and out["unanswered"] == 0
    errors = ws.http(config, sample, errors=True)
    assert [(r["uri"], r["status"]) for r in errors["rows"]] == [("/favicon.ico", 404), ("/api/orders?day=today", 500)]
    slow = ws.http(config, sample, slow_ms=20)
    assert [r["status"] for r in slow["rows"]] == [500]


def test_tls(config, sample) -> None:
    out = ws.tls(config, sample)
    assert out["handshakes"] == 3 and out["failed"] == 1 and out["alerts"] == {"unknown_ca": 1}
    bad = out["connections"][0]
    assert bad["stream"] == 7 and not bad["ok"] and bad["alerts"][0]["alert"] == "unknown_ca"
    v12 = next(c for c in out["connections"] if c["stream"] == 6)
    assert v12["version"] == "TLS 1.2" and v12["cipher"] == "ECDHE-RSA-AES256-GCM-SHA384" and "api.example.com" in v12["cert_names"]
    assert [c["stream"] for c in ws.tls(config, sample, failed=True)["connections"]] == [7]


def test_summary(config, sample) -> None:
    out = ws.summary(config, sample)
    assert out["info"]["packets"] == 96 and out["tcp"] == {"connections": 8, "reset": 4}
    assert "tcp.segments" not in [p["protocol"] for p in out["protocols"]]
    assert out["expert"]["warning"] == 4 and out["expert_top"][0]["message"] == "Connection reset (RST)"
    assert len(json.dumps(out)) < 4000


def test_save_filtered_and_merge(config, sample, tmp_path) -> None:
    one = tmp_path / "s2.pcapng"
    out = ws.save_filtered(config, sample, "tcp.stream == 2", str(one))
    assert out["packets"] == 12 and one.is_file()
    with pytest.raises(WiresharkError, match="--overwrite"):
        ws.save_filtered(config, sample, "tcp.stream == 2", str(one))
    with pytest.raises(ValueError):
        ws.save_filtered(config, sample, "tcp", sample)
    if shutil.which("mergecap"):
        merged = ws.merge(config, [sample, str(one)], str(tmp_path / "m.pcapng"))
        assert merged["packets"] == 108
    with pytest.raises(ValueError):
        ws.merge(config, [sample], str(tmp_path / "x.pcapng"))


def test_export_objects(config, sample, tmp_path) -> None:
    out = ws.export_objects(config, sample, str(tmp_path / "objs"))
    assert out["total"] == 5 and out["objects"][0]["bytes"] == 48
    with pytest.raises(WiresharkError, match="not empty"):
        ws.export_objects(config, sample, str(tmp_path / "objs"))


def test_capture_validates_before_running(config, tmp_path) -> None:
    with pytest.raises(ValueError):
        ws.capture(config, "lo", duration=0)
    existing = tmp_path / "c.pcapng"
    existing.write_text("x")
    with pytest.raises(WiresharkError, match="--overwrite"):
        ws.capture(config, "lo", output=str(existing))


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="live capture needs root")
def test_capture_live(config, tmp_path) -> None:
    out = ws.capture(config, "lo", duration=1, output=str(tmp_path / "live.pcapng"), capture_filter="host 192.0.2.1")
    assert out["file"].endswith("live.pcapng") and out["packets"] == 0 and "hint" in out


def test_timeout(sample) -> None:
    with pytest.raises(WiresharkError, match="took longer"):
        # 1 ms is too short for any tshark start-up.
        ws.summary(WiresharkConfig(timeout=0.001), sample)

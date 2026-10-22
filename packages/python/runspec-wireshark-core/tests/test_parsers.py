"""Pure parsers and helpers — no tshark needed."""

from __future__ import annotations

import os

import pytest

from runspec_wireshark_core import WiresharkConfig, WiresharkError, config_from_env, find_tool, parse_capinfos, parse_follow, parse_interfaces, parse_phs, parse_verbose
from runspec_wireshark_core import client as client_mod
from runspec_wireshark_core.client import ToolNotFoundError, _failure, output_path, shorten
from runspec_wireshark_core.stats import pick_interval


def test_parse_interfaces_marks_extcap_and_virtual() -> None:
    rows = parse_interfaces("1. eth0\n2. any\n3. lo (Loopback)\n4. nflog\n5. sshdump (SSH remote capture)\n6. \\Device\\NPF_{ABC} (Ethernet 2)\n")
    assert [r["name"] for r in rows] == ["eth0", "any", "lo", "nflog", "sshdump", "\\Device\\NPF_{ABC}"]
    assert rows[2]["description"] == "Loopback"
    assert [r["kind"] for r in rows] == ["network", "network", "network", "virtual", "extcap", "network"]
    assert rows[5]["description"] == "Ethernet 2"


def test_parse_capinfos_converts_types_and_utc() -> None:
    text = (
        '"File name","File type","File encapsulation","Packet size limit","Number of packets","File size (bytes)",'
        '"Capture duration (seconds)","Start time","End time","Strict time order","Capture comment"\n'
        '"x.pcapng","pcapng","ether","(not set)","96","17812","0.581327","1792674520.251520","1792674520.832847","True",""\n'
    )
    out = parse_capinfos(text)
    assert out["packets"] == 96 and out["file_bytes"] == 17812 and out["duration_s"] == 0.581327
    assert out["start"] == "2026-10-22T13:08:40.251520Z" and out["time_ordered"] is True
    assert "snaplen" not in out and "comment" not in out


def test_parse_capinfos_rejects_garbage() -> None:
    with pytest.raises(WiresharkError):
        parse_capinfos("nope")


def test_parse_phs_depths() -> None:
    text = (
        "===\nProtocol Hierarchy Statistics\nFilter: \n\n"
        "eth   frames:10 bytes:1000\n  ip   frames:10 bytes:1000\n    tcp  frames:7 bytes:800\n      tls frames:3 bytes:500\n    udp  frames:3 bytes:200\n===\n"
    )
    rows = parse_phs(text)
    assert [(r["protocol"], r["depth"]) for r in rows] == [("eth", 0), ("ip", 1), ("tcp", 2), ("tls", 3), ("udp", 2)]
    assert rows[3]["bytes"] == 500


FOLLOW = (
    "\n===================================================================\n"
    "Follow: tcp,ascii\nFilter: tcp.stream eq 0\nNode 0: 10.0.0.1:5000\nNode 1: 10.0.0.2:80\n"
    "16\nGET / HTTP/1.1\r\n\n"
    "\t28\n200\nNode 1: fake\n12345678901\n"
    "===================================================================\n"
)


def test_parse_follow_uses_byte_counts() -> None:
    # The second chunk's payload holds a bare number and a "Node 1:" line —
    # counting bytes (not reading line shapes) keeps them as payload.
    parsed = parse_follow(FOLLOW)
    assert parsed["nodes"] == ["10.0.0.1:5000", "10.0.0.2:80"]
    assert [c["from"] for c in parsed["chunks"]] == ["10.0.0.1:5000", "10.0.0.2:80"]
    assert parsed["chunks"][0]["text"] == "GET / HTTP/1.1\r\n"
    assert parsed["chunks"][1]["text"] == "200\nNode 1: fake\n12345678901"


def test_parse_follow_empty() -> None:
    assert parse_follow("===\nFollow: tcp,ascii\n===\n") == {"nodes": [], "chunks": []}


def test_parse_verbose_depth_bits_and_frame() -> None:
    text = (
        "Frame 1: 60 bytes on wire\n    Section number: 1\n    Arrival Time: Oct 1\n    Frame Length: 60 bytes\n"
        "Internet Protocol Version 4, Src: 1.1.1.1, Dst: 2.2.2.2\n    0100 .... = Version: 4\n    Flags: 0x2\n        .1.. .... = Don't fragment: Set\n"
    )
    layers = parse_verbose(text, depth=1)
    assert layers[0]["lines"] == ["Arrival Time: Oct 1", "Frame Length: 60 bytes"]
    assert layers[1]["lines"] == ["Version: 4", "Flags: 0x2"]
    deep = parse_verbose(text, depth=2, full_frame=True)
    assert deep[0]["lines"][0] == "Section number: 1"
    assert deep[1]["lines"][-1] == "  Don't fragment: Set"
    titles = parse_verbose(text, depth=0)
    assert all("lines" not in layer for layer in titles)


def test_parse_verbose_caps_lines_per_layer() -> None:
    text = "TCP\n" + "".join(f"    line {i}\n" for i in range(10))
    layer = parse_verbose(text, max_lines=3)[0]
    assert layer["lines"] == ["line 0", "line 1", "line 2"] and layer["more"] == 7


def test_pick_interval() -> None:
    assert pick_interval(0.58, 60) == 0.01
    assert pick_interval(3600, 60) == 60
    assert pick_interval(0, 60) == 0.001
    assert pick_interval(10_000_000, 60) == 166667


def test_failure_messages() -> None:
    bad = _failure(["/usr/bin/tshark", "-r", "x", "-Y", "a = = b"], 4, 'Running as user "root"\ntshark: "=" was unexpected in this context.\n    a = = b\n      ^')
    assert str(bad).startswith('bad display filter: "=" was unexpected')
    z = _failure(["tshark", "-z", "bogus"], 1, 'tshark: Invalid -z argument "bogus"; it must be one of:\n     afp,srt\n     io,phs\n')
    assert str(z) == 'Invalid -z argument "bogus" — see `tshark -z help` for the list'
    other = _failure(["C:\\Wireshark\\tshark.exe", "-r", "x"], 2, "tshark: The file x isn't a capture file")
    assert str(other).startswith("tshark exited 2: The file x")


def test_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("TSHARK_PATH", "/opt/ws/tshark")
    monkeypatch.setenv("SSLKEYLOGFILE", "/tmp/keys")
    monkeypatch.setenv("WIRESHARK_TIMEOUT", "30")
    cfg = config_from_env()
    assert (cfg.tshark, cfg.keylog_file, cfg.timeout) == ("/opt/ws/tshark", "/tmp/keys", 30.0)
    monkeypatch.setenv("WIRESHARK_KEYLOG_FILE", "/tmp/wkeys")
    assert config_from_env().keylog_file == "/tmp/wkeys"
    assert config_from_env(tshark="/x/tshark", timeout=5).tshark == "/x/tshark"


def test_find_tool_explicit_missing() -> None:
    with pytest.raises(ToolNotFoundError, match="--tshark"):
        find_tool("tshark", WiresharkConfig(tshark="/nonexistent/tshark"))


def test_find_tool_companion_next_to_tshark(tmp_path, monkeypatch) -> None:
    for name in ("tshark", "capinfos"):
        exe = tmp_path / (name + (".exe" if os.name == "nt" else ""))
        exe.write_text("")
    monkeypatch.setattr(client_mod.shutil, "which", lambda name: None)
    cfg = WiresharkConfig(tshark=str(tmp_path / ("tshark.exe" if os.name == "nt" else "tshark")))
    assert find_tool("capinfos", cfg).startswith(str(tmp_path))
    with pytest.raises(ToolNotFoundError, match="install"):
        monkeypatch.setattr(client_mod, "_KNOWN_DIRS", {})
        find_tool("mergecap", cfg)


def test_output_path_refuses_existing(tmp_path) -> None:
    existing = tmp_path / "a.pcapng"
    existing.write_text("x")
    with pytest.raises(WiresharkError, match="--overwrite"):
        output_path(existing, overwrite=False)
    assert output_path(existing, overwrite=True) == str(existing)
    assert output_path(tmp_path / "new" / "b.pcapng", overwrite=False).endswith("b.pcapng")
    assert (tmp_path / "new").is_dir()


def test_shorten() -> None:
    assert shorten("abcdef", 4) == "abc…" and shorten("abc", 4) == "abc" and shorten("abcdef", 0) == "abcdef"


def test_missing_capture_file(config) -> None:
    from runspec_wireshark_core import packets

    with pytest.raises(WiresharkError, match="not found"):
        packets(config, "/nonexistent/x.pcapng")

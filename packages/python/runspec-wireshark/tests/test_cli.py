"""Wrapper logic: real parsed spec → `_dispatch` → core helper → real tshark on
the sample capture (skipped without tshark), plus main()'s output paths."""

import json
import shutil
from pathlib import Path

import pytest
import runspec
from runspec.testing import ParseHarness, no_run_as_check
from runspec_wireshark_core import WiresharkConfig, WiresharkError

from runspec_wireshark import cli

SPEC = Path(__file__).parent.parent / "runspec_wireshark" / "runspec.toml"
SAMPLE = Path(__file__).resolve().parents[2] / "runspec-wireshark-core" / "tests" / "data" / "sample.pcapng"
needs_sample = pytest.mark.skipif(shutil.which("tshark") is None or not SAMPLE.is_file(), reason="needs tshark and the sample capture")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("TSHARK_PATH", "WIRESHARK_TSHARK", "WIRESHARK_KEYLOG_FILE", "SSLKEYLOGFILE", "WIRESHARK_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RUNSPEC_AUDIT", "off")


def _run(argv: list[str], config: WiresharkConfig | None = None):
    h = ParseHarness(script_name="pcap", config_path=SPEC)
    spec = h.expect_ok(argv)
    try:
        return cli._dispatch(spec, config or WiresharkConfig())
    finally:
        h.close()


@needs_sample
@pytest.mark.parametrize(
    ("argv", "check"),
    [
        (["summary", str(SAMPLE)], lambda out: out["tcp"]["reset"] == 4),
        (["packets", str(SAMPLE), "-Y", "http.response.code >= 400"], lambda out: out["total"] == 2),
        (["packet", str(SAMPLE), "95", "--layer", "tls"], lambda out: "Unknown CA" in out["layers"][-1]["lines"][0]),
        (["fields", str(SAMPLE), "--field", "http.host,http.request.uri", "-Y", "http.request"], lambda out: out["rows"][2]["http.request.uri"] == "/api/orders?day=today"),
        (["top", str(SAMPLE), "ip.src"], lambda out: out["values"][0]["value"] == "10.20.0.15"),
        (["conversations", str(SAMPLE), "--type", "udp"], lambda out: out["total"] == 3),
        (["follow", str(SAMPLE), "2"], lambda out: out["chunks"][-1]["text"] == '{"error":"database timeout"}'),
        (["http", str(SAMPLE), "--errors"], lambda out: [r["status"] for r in out["rows"]] == [404, 500]),
        (["tls", str(SAMPLE), "--failed"], lambda out: out["connections"][0]["alerts"][0]["alert"] == "unknown_ca"),
        (["dns", str(SAMPLE), "--failed"], lambda out: out["count"] == 2),
    ],
)
def test_dispatch(argv, check) -> None:
    assert check(_run(argv))


@needs_sample
def test_keylog_global_reaches_tshark(monkeypatch) -> None:
    keys = SAMPLE.with_name("sample.keys")
    out = _run(["--keylog-file", str(keys), "follow", str(SAMPLE), "5", "--protocol", "tls"], WiresharkConfig(keylog_file=str(keys)))
    assert any("healthy" in c["text"] for c in out["chunks"])


def test_capture_is_audited(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(cli, "capture", lambda config, interface, **kw: {"file": "/tmp/c.pcapng", "interface": interface, "packets": 7, "file_bytes": 900})
    monkeypatch.setattr(cli.rs, "audit", lambda event, outcome, **kw: seen.append((event, outcome, kw)))
    out = _run(["capture", "-i", "eth0", "-f", "port 443"])
    assert out["packets"] == 7
    event, outcome, kw = seen[0]
    assert (event, outcome) == ("pcap.capture", "succeeded")
    assert kw["target"] == {"interface": "eth0"} and kw["detail"]["capture_filter"] == "port 443" and kw["detail"]["packets"] == 7


def test_failed_capture_is_audited(monkeypatch) -> None:
    seen = []

    def boom(config, interface, **kw):
        raise WiresharkError("You don't have permission to capture on that device")

    monkeypatch.setattr(cli, "capture", boom)
    monkeypatch.setattr(cli.rs, "audit", lambda event, outcome, **kw: seen.append((event, outcome)))
    with pytest.raises(WiresharkError):
        _run(["capture", "-i", "eth0"])
    assert seen == [("pcap.capture", "failed")]


def _main(monkeypatch, capsys, argv, *, terminal=False):
    monkeypatch.setattr("sys.argv", ["pcap", *argv])
    monkeypatch.setattr(cli, "_terminal", lambda: terminal)
    code = 0
    with no_run_as_check():
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code or 0
    return code, capsys.readouterr().out


@needs_sample
def test_main_prints_compact_json(monkeypatch, capsys) -> None:
    code, out = _main(monkeypatch, capsys, ["file-info", str(SAMPLE)])
    assert code == 0
    assert json.loads(out)["packets"] == 96 and ": " in out and "\n " not in out


@needs_sample
def test_main_prints_text_at_a_terminal(monkeypatch, capsys) -> None:
    code, out = _main(monkeypatch, capsys, ["follow", str(SAMPLE), "2"], terminal=True)
    assert code == 0 and out.startswith("tcp stream 2: 10.20.0.15:43807 ⇄ 10.20.1.80:8080")


def test_main_error_payload(monkeypatch, capsys) -> None:
    code, out = _main(monkeypatch, capsys, ["file-info", "/nonexistent/x.pcapng"])
    assert code == 1
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["kind"] in ("WiresharkError", "ToolNotFoundError")


def test_runspec_parse_finds_the_spec() -> None:
    spec = runspec.parse("pcap", argv=["check"], config_path=SPEC)
    assert spec.runspec_command == "check"

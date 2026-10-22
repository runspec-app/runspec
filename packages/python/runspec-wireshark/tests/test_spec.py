"""Wiring / parsing tests for the `pcap` runnable — no mocks, real spec."""

import sys
from pathlib import Path

from runspec.testing import ParseHarness

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

SPEC = Path(__file__).parent.parent / "runspec_wireshark" / "runspec.toml"
WRITES = {"capture", "save-filtered", "merge", "export-objects"}


def _harness() -> ParseHarness:
    return ParseHarness(script_name="pcap", config_path=SPEC)


def test_smoke_all_verbs() -> None:
    h = _harness()
    h.smoke(
        values={
            "file": "x.pcapng",
            "number": 1,
            "field": "ip.src",
            "stat": "http,tree",
            "interface": "eth0",
            "filter": "tcp",
            "output": "out.pcapng",
            "input": "a.pcapng",
            "directory": "objs",
        }
    )
    h.close()


def test_autonomy_tiers() -> None:
    commands = tomllib.loads(SPEC.read_text())["pcap"]["commands"]
    tiers = {name: body["autonomy"] for name, body in commands.items()}
    assert {n for n, t in tiers.items() if t != "autonomous"} == WRITES
    assert all(tiers[n] == "confirm" for n in WRITES)


def test_file_and_stream_are_positional() -> None:
    h = _harness()
    spec = h.expect_ok(["follow", "/tmp/a.pcapng", "3", "--protocol", "tls"])
    assert str(spec.file.value) == "/tmp/a.pcapng" and spec.stream.value == 3 and spec.protocol.value == "tls"
    assert h.expect_ok(["follow", "/tmp/a.pcapng"]).stream.value == 0
    spec = h.expect_ok(["packet", "/tmp/a.pcapng", "95", "--layer", "ip,tcp", "--include", "bytes"])
    assert spec.number.value == 95 and list(spec.layer.value) == ["ip", "tcp"] and list(spec.include.value) == ["bytes"]
    h.expect_exit(["packet", "/tmp/a.pcapng"], contains="number")
    h.expect_exit(["packet", "/tmp/a.pcapng", "1", "--include", "everything"], contains="include")
    h.close()


def test_filter_short_flag_and_defaults() -> None:
    h = _harness()
    spec = h.expect_ok(["packets", "a.pcapng", "-Y", "tcp.flags.reset == 1"])
    assert spec.filter.value == "tcp.flags.reset == 1" and spec.limit.value == 25 and spec.offset.value == 0
    assert h.expect_ok(["conversations", "a.pcapng"]).type.value == "tcp"
    h.expect_exit(["conversations", "a.pcapng", "--type", "sctp"], contains="type")
    h.close()


def test_globals() -> None:
    h = _harness()
    spec = h.expect_ok(["--keylog-file", "/k.log", "--decode-as", "tcp.port==8888,http", "--decode-as", "udp.port==5555,sip", "--timeout", "30", "summary", "a.pcapng"])
    assert spec.keylog_file.value == "/k.log" and list(spec.decode_as.value) == ["tcp.port==8888,http", "udp.port==5555,sip"] and spec.timeout.value == 30
    h.expect_exit(["--timeout", "0", "summary", "a.pcapng"], contains="timeout")
    h.close()


def test_capture_args() -> None:
    h = _harness()
    spec = h.expect_ok(["capture", "-i", "eth0", "-f", "port 443", "-w", "/tmp/c.pcapng"])
    assert spec.interface.value == "eth0" and spec.capture_filter.value == "port 443" and spec.duration.value == 10 and spec.max_mb.value == 100
    h.expect_exit(["capture", "-i", "eth0", "--duration", "99999"], contains="duration")
    h.expect_exit(["capture"], contains="interface")
    h.close()


def test_merge_takes_repeated_inputs() -> None:
    h = _harness()
    spec = h.expect_ok(["merge", "--input", "a.pcapng", "--input", "b.pcapng", "-w", "m.pcapng"])
    assert list(spec.input.value) == ["a.pcapng", "b.pcapng"]
    h.close()


def test_globals_declare_the_env_vars_the_core_reads() -> None:
    # runspec-console's Env files window and unscoped settings only reach
    # names a runnable declares; these are what config_from_env reads.
    args = tomllib.loads(SPEC.read_text())["pcap"]["args"]

    def envs(name: str) -> list[str]:
        env = args[name]["env"]
        return [env] if isinstance(env, str) else env

    assert envs("tshark") == ["TSHARK_PATH", "WIRESHARK_TSHARK"]
    assert envs("keylog-file") == ["WIRESHARK_KEYLOG_FILE", "SSLKEYLOGFILE"]
    assert envs("timeout") == ["WIRESHARK_TIMEOUT"]


def test_capture_dir_declares_its_env_var() -> None:
    capture = tomllib.loads(SPEC.read_text())["pcap"]["commands"]["capture"]["args"]
    assert capture["capture-dir"]["env"] == "WIRESHARK_CAPTURE_DIR"

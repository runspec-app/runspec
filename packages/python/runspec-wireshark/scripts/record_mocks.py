"""Re-record runspec-wireshark's shipped mock responses (runspec_wireshark/mocks/pcap.json).

Runs each call below for real with ``runspec mock record`` against the sample
capture in ``runspec-wireshark-core/tests/data`` (made-up traffic on made-up
addresses — see that package's ``scripts/make_sample_capture.py``), so the
responses come from the real code and a real tshark.

    # from packages/python/runspec-wireshark, in a venv with runspec,
    # runspec-wireshark-core and runspec-wireshark installed editable, and tshark on PATH
    python scripts/record_mocks.py

Run as root (with scapy installed) to also record ``capture``: the sample's
packets are replayed onto the loopback interface while ``pcap capture -i lo``
records them.

Afterwards the temp folder is replaced with ``/var/tmp`` and the file-path args
are dropped from each entry's key, so e.g. ``pcap follow <any file> 2`` gets the
recorded answer for stream 2.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLE = HERE.parent.parent / "runspec-wireshark-core" / "tests" / "data"
MOCK_FILE = HERE.parent / "runspec_wireshark" / "mocks" / "pcap.json"
PUBLIC_DIR = "/var/tmp"
# Args that name a local file: dropped from the key so any path matches.
PATH_ARGS = {"file", "output", "directory", "input", "keylog-file", "keylog_file", "tshark"}
# Verbs whose recorded answer stands for any call: the key is emptied.
ANY_CALL = {"save-filtered", "capture"}


def calls(cap: str, keys: str, tmp: str) -> list[tuple[list[str], str]]:
    return [
        (["check"], ""),
        (["list-interfaces"], ""),
        (["file-info", cap], ""),
        (["summary", cap], "any capture: the sample office traffic"),
        (["packets", cap], "any capture: the first 25 packets"),
        (["packets", cap, "--filter", "tcp.flags.reset == 1"], ""),
        (["packets", cap, "--filter", "http.response.code >= 400"], ""),
        (["packet", cap, "95"], "the TLS unknown_ca alert"),
        (["packet", cap, "34", "--layer", "http"], "the request that got a 500"),
        (["fields", cap, "--field", "ip.src,ip.dst,tcp.dstport,_ws.col.Info", "--limit", "10"], ""),
        (["top", cap, "ip.dst", "--by", "bytes"], ""),
        (["top", cap, "tcp.dstport"], ""),
        (["protocols", cap], ""),
        (["conversations", cap], "any capture: the TCP connections"),
        (["conversations", cap, "--type", "ip"], ""),
        (["endpoints", cap], ""),
        (["expert", cap], ""),
        (["expert", cap, "--severity", "note"], ""),
        (["io", cap], ""),
        (["stats", cap, "http,tree"], ""),
        (["stats", cap, "dns,tree"], ""),
        (["follow", cap, "0"], "GET / → 200"),
        (["follow", cap, "2"], "GET /api/orders → 500 database timeout"),
        (["follow", cap, "3"], "POST /api/orders → 201"),
        (["--keylog-file", keys, "follow", cap, "5", "--protocol", "tls"], "decrypted with the session keys"),
        (["dns", cap], ""),
        (["dns", cap, "--failed"], ""),
        (["http", cap], ""),
        (["http", cap, "--errors"], ""),
        (["tls", cap], ""),
        (["tls", cap, "--failed"], ""),
        (["save-filtered", cap, "--filter", "tcp.stream == 2", "--output", f"{tmp}/stream2.pcapng"], "any filter and output"),
        (["merge", "--input", cap, "--input", f"{tmp}/stream2.pcapng", "--output", f"{tmp}/merged.pcapng"], "any inputs and output"),
        (["export-objects", cap, "--directory", f"{tmp}/objects"], "any capture and folder: the HTTP bodies"),
    ]


def _replay_on_loopback(cap: str, delay: float) -> None:
    from scapy.all import rdpcap, sendp  # type: ignore[import-not-found]

    time.sleep(delay)
    sendp(rdpcap(cap), iface="lo", verbose=False)


def main() -> None:
    runspec = shutil.which("runspec") or "runspec"
    env = {**os.environ, "RUNSPEC_AUDIT": "off"}
    env.pop("SSLKEYLOGFILE", None)
    failed = 0
    # A clean set: every entry below is recorded afresh.
    MOCK_FILE.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        cap, keys = f"{tmp}/outage.pcapng", f"{tmp}/outage.keys"
        shutil.copy(SAMPLE / "sample.pcapng", cap)
        shutil.copy(SAMPLE / "sample.keys", keys)
        todo = calls(cap, keys, tmp)
        if os.geteuid() == 0:
            # Replay the sample onto lo while capturing it.
            todo.append((["capture", "-i", "lo", "--duration", "4", "-f", "net 10.20.0.0/16", "--output", f"{tmp}/capture.pcapng"], "any interface: the sample traffic, captured live"))
        for argv, note in todo:
            if argv[0] == "capture":
                threading.Thread(target=_replay_on_loopback, args=(cap, 1.5), daemon=True).start()
            cmd = [runspec, "mock", "record", "pcap", "--to", "package"]
            if note:
                cmd += ["--note", note]
            rc = subprocess.call([*cmd, "--", *argv], env=env)
            failed += rc != 0
        _tidy(tmp)
    sys.exit(1 if failed else 0)


def _tidy(tmp: str) -> None:
    data = json.loads(MOCK_FILE.read_text(encoding="utf-8").replace(tmp, PUBLIC_DIR))
    for entry in data["entries"]:
        entry["args"] = {} if entry["command"][0] in ANY_CALL else {k: v for k, v in entry["args"].items() if k not in PATH_ARGS}
    MOCK_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    MOCK_FILE.chmod(0o644)


if __name__ == "__main__":
    main()

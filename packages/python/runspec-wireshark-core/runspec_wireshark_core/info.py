"""info.py — the install, the capture interfaces, and a capture file's metadata."""

from __future__ import annotations

import csv
import io
import os
import re
from datetime import datetime, timezone
from typing import Any

from runspec_wireshark_core.client import WiresharkConfig, WiresharkError, capture_file, find_tool, iter_fields, run, to_float, to_int

# extcap "interfaces" tshark lists next to the real ones: remote-capture
# helpers and generators, rarely what anyone wants to capture on.
EXTCAP = {"androiddump", "ciscodump", "dpauxmon", "etwdump", "randpkt", "sdjournal", "sshdump", "udpdump", "wifidump", "falcodump"}
_VIRTUAL = re.compile(r"^(bluetooth|dbus|nflog|nfqueue|usbmon|ifb\d)", re.IGNORECASE)


def check(config: WiresharkConfig) -> dict[str, Any]:
    """Which tools are installed, where, and the tshark version."""
    tshark = find_tool("tshark", config)
    first_line = run([tshark, "--version"], timeout=30).splitlines()[0]
    match = re.search(r"\b(\d+\.\d+\.\d+)", first_line)
    out: dict[str, Any] = {"tshark": tshark, "version": match.group(1) if match else first_line}
    for name in ("capinfos", "mergecap", "dumpcap"):
        try:
            out[name] = find_tool(name, config)
        except WiresharkError:
            out[name] = None
    if config.keylog_file:
        out["keylog_file"] = config.keylog_file
        out["keylog_file_exists"] = os.path.isfile(config.keylog_file)
    return out


def parse_interfaces(text: str) -> list[dict[str, Any]]:
    """``tshark -D`` lines (``3. lo (Loopback)``) → rows."""
    rows = []
    for line in text.splitlines():
        match = re.match(r"^\s*(\d+)\.\s+(\S+)(?:\s+\((.*)\))?\s*$", line)
        if not match:
            continue
        index, name, description = match.groups()
        row: dict[str, Any] = {"index": int(index), "name": name}
        if description:
            row["description"] = description
        row["kind"] = "extcap" if name in EXTCAP else "virtual" if _VIRTUAL.match(name) else "network"
        rows.append(row)
    return rows


def list_interfaces(config: WiresharkConfig, *, all: bool = False) -> dict[str, Any]:
    """Interfaces tshark can capture on. Network interfaces only unless
    ``all`` (then the extcap helpers and virtual ones too)."""
    rows = parse_interfaces(run([find_tool("tshark", config), "-D"], timeout=60))
    shown = rows if all else [r for r in rows if r["kind"] == "network"]
    return {"count": len(shown), "hidden": len(rows) - len(shown), "interfaces": shown}


_CAPINFOS_KEYS = {
    "File type": "file_type",
    "File encapsulation": "encapsulation",
    "Packet size limit": "snaplen",
    "Number of packets": "packets",
    "File size (bytes)": "file_bytes",
    "Data size (bytes)": "data_bytes",
    "Capture duration (seconds)": "duration_s",
    "Start time": "start",
    "End time": "end",
    "Data byte rate (bytes/sec)": "bytes_per_s",
    "Average packet size (bytes)": "avg_packet_bytes",
    "Average packet rate (packets/sec)": "packets_per_s",
    "Strict time order": "time_ordered",
    "Capture comment": "comment",
}


def utc(epoch: float) -> str:
    """Seconds since the epoch → ISO 8601 UTC, to the microsecond."""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _number(text: str) -> Any:
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            pass
    return text


def parse_capinfos(text: str) -> dict[str, Any]:
    """``capinfos -S -T -m -Q`` (a CSV header + one row) → compact dict, with
    the start/end times (epoch seconds under ``-S``) as ISO 8601 UTC."""
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise WiresharkError(f"unexpected capinfos output: {text[:200]!r}")
    out: dict[str, Any] = {}
    for key, value in zip(rows[0], rows[1], strict=False):
        name = _CAPINFOS_KEYS.get(key)
        if name and value not in ("", "n/a", "(not set)"):
            if name == "time_ordered":
                out[name] = value == "True"
            elif name in ("start", "end"):
                out[name] = utc(float(value)) if re.fullmatch(r"\d+(\.\d+)?", value) else value
            elif name in ("comment", "file_type", "encapsulation"):
                out[name] = value
            else:
                out[name] = _number(value)
    return out


def file_info(config: WiresharkConfig, file: str | os.PathLike[str]) -> dict[str, Any]:
    """Packets, size, duration, start/end time, encapsulation of a capture.

    Uses ``capinfos`` (fast: reads only the headers it needs); falls back to one
    tshark pass when capinfos isn't installed.
    """
    path = capture_file(file)
    try:
        capinfos = find_tool("capinfos", config)
    except WiresharkError:
        capinfos = None
    if capinfos:
        out = parse_capinfos(run([capinfos, "-t", "-E", "-c", "-s", "-d", "-u", "-a", "-e", "-y", "-i", "-z", "-x", "-l", "-o", "-k", "-S", "-T", "-m", "-Q", path], timeout=config.timeout))
        return {"file": path, **out}
    packets = data = 0
    start = end = None
    for _number, epoch, length in iter_fields(config, path, ["frame.number", "frame.time_epoch", "frame.len"], occurrence="f"):
        packets += 1
        data += to_int(length) or 0
        when = to_float(epoch)
        if when is not None:
            start = when if start is None else min(start, when)
            end = when if end is None else max(end, when)
    out = {"file": path, "packets": packets, "file_bytes": os.path.getsize(path), "data_bytes": data}
    if start is not None and end is not None:
        out["duration_s"] = round(end - start, 6)
        out["start"], out["end"] = utc(start), utc(end)
    return out

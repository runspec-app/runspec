"""stats.py — protocol hierarchy, conversations, endpoints, traffic over time,
expert info, raw ``-z`` statistics, and a one-call summary.

Conversations, endpoints and the I/O buckets are counted here from one
``-T fields`` pass rather than parsed from tshark's ``-z conv`` tables: those
print bytes with units (``2 kB``) that change between versions, and they don't
carry the TCP stream index ``follow`` needs.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from runspec_wireshark_core.client import WiresharkConfig, WiresharkError, every, iter_fields, read_argv, run, shorten, to_float, to_int
from runspec_wireshark_core.info import file_info
from runspec_wireshark_core.packets import endpoint

CONVERSATION_TYPES = ("ip", "tcp", "udp", "eth")
SEVERITIES = ("comment", "chat", "note", "warning", "error")
# tshark's PI_* severity values, for a `_ws.expert.severity >= N` filter.
SEVERITY_VALUE = {"comment": 0x00100000, "chat": 0x00200000, "note": 0x00400000, "warning": 0x00600000, "error": 0x00800000}
_TCP_FLAGS = {0x01: "FIN", 0x02: "SYN", 0x04: "RST"}
# `-z` statistics that print secrets; `credentials` lists every password it finds.
REFUSED_STATS = ("credentials",)


# ── protocol hierarchy ─────────────────────────────────────────────────────


def parse_phs(text: str) -> list[dict[str, Any]]:
    """``-z io,phs`` text → ``[{protocol, depth, frames, bytes}]`` in tree order
    (depth 0 = the link layer)."""
    rows = []
    for line in text.splitlines():
        match = re.match(r"^(\s*)(\S+)\s+frames:(\d+)\s+bytes:(\d+)\s*$", line)
        if match:
            indent, proto, frames, size = match.groups()
            rows.append({"protocol": proto, "depth": len(indent) // 2, "frames": int(frames), "bytes": int(size)})
    return rows


def protocols(config: WiresharkConfig, file: str | os.PathLike[str], *, filter: str | None = None, max_depth: int = 0) -> dict[str, Any]:
    """Which protocols the capture holds and how much of it each is (frames
    and bytes), as a tree flattened in order. ``max_depth`` (0 = all) drops
    the deeper levels."""
    stat = "io,phs" + (f",{filter}" if filter else "")
    rows = parse_phs(run(read_argv(config, file) + ["-q", "-z", stat], timeout=config.timeout))
    total = rows[0]["frames"] if rows else 0
    for row in rows:
        row["percent"] = round(100 * row["frames"] / total, 1) if total else 0.0
    if max_depth:
        rows = [r for r in rows if r["depth"] < max_depth]
    out: dict[str, Any] = {"file": os.fspath(file)}
    if filter:
        out["filter"] = filter
    return {**out, "frames": total, "protocols": rows}


# ── one pass for conversations / endpoints / expert ────────────────────────

_FLOW_FIELDS = [
    "frame.len",
    "frame.time_relative",
    "eth.src",
    "eth.dst",
    "ip.src",
    "ip.dst",
    "ipv6.src",
    "ipv6.dst",
    "tcp.stream",
    "tcp.srcport",
    "tcp.dstport",
    "tcp.flags",
    "udp.stream",
    "udp.srcport",
    "udp.dstport",
]


@dataclass
class _Conv:
    a: str
    b: str
    start: float
    end: float
    packets_ab: int = 0
    bytes_ab: int = 0
    packets_ba: int = 0
    bytes_ba: int = 0
    flags: set[str] = field(default_factory=set)
    stream: int | None = None

    def add(self, forward: bool, size: int, when: float) -> None:
        self.end = max(self.end, when)
        if forward:
            self.packets_ab += 1
            self.bytes_ab += size
        else:
            self.packets_ba += 1
            self.bytes_ba += size

    def row(self, kind: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.stream is not None:
            out["stream"] = self.stream
        out.update(
            {
                "a": self.a,
                "b": self.b,
                "packets": self.packets_ab + self.packets_ba,
                "bytes": self.bytes_ab + self.bytes_ba,
                "a_to_b": {"packets": self.packets_ab, "bytes": self.bytes_ab},
                "b_to_a": {"packets": self.packets_ba, "bytes": self.bytes_ba},
                "start": round(self.start, 6),
                "duration": round(self.end - self.start, 6),
            }
        )
        if kind == "tcp":
            out["flags"] = [name for name in ("SYN", "FIN", "RST") if name in self.flags]
        return out


@dataclass
class _Endpoint:
    tx_packets: int = 0
    tx_bytes: int = 0
    rx_packets: int = 0
    rx_bytes: int = 0


@dataclass
class Flows:
    """What one fields pass counted."""

    packets: int = 0
    bytes: int = 0
    convs: dict[str, dict[Any, _Conv]] = field(default_factory=lambda: {k: {} for k in CONVERSATION_TYPES})
    endpoints: dict[str, dict[str, _Endpoint]] = field(default_factory=lambda: {k: {} for k in CONVERSATION_TYPES})
    expert: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    expert_first: dict[tuple[str, str, str], int] = field(default_factory=dict)


_EXPERT = re.compile(r"^Expert Info \((\w+)/([^)]*)\): (.*)$", re.DOTALL)


def _severity(word: str) -> str:
    word = word.lower()
    return {"warn": "warning", "warns": "warning", "errors": "error", "notes": "note", "chats": "chat"}.get(word, word)


def scan(config: WiresharkConfig, file: str | os.PathLike[str], *, filter: str | None = None, expert: bool = False) -> Flows:
    """One pass over the capture counting conversations and endpoints of
    every type (and, with ``expert``, expert-info messages)."""
    names = [*_FLOW_FIELDS, *(["frame.number", "_ws.expert"] if expert else [])]
    flows = Flows()
    for values in iter_fields(config, file, names, filter=filter, occurrence="f" if not expert else "a"):
        size = to_int(values[0]) or 0
        when = to_float(values[1]) or 0.0
        eth_s, eth_d, ip_s, ip_d, ip6_s, ip6_d = (v.split("\x1f")[0] for v in values[2:8])
        tstream, tsp, tdp, tflags, ustream, usp, udp_ = (v.split("\x1f")[0] for v in values[8:15])
        flows.packets += 1
        flows.bytes += size
        net_s, net_d = (ip_s, ip_d) if ip_s else (ip6_s, ip6_d)
        layers: list[tuple[str, Any, str, str]] = []
        if eth_s and eth_d:
            layers.append(("eth", frozenset((eth_s, eth_d)), eth_s, eth_d))
        if net_s and net_d:
            layers.append(("ip", frozenset((net_s, net_d)), net_s, net_d))
            if tstream:
                layers.append(("tcp", int(tstream), endpoint(net_s, tsp), endpoint(net_d, tdp)))
            elif ustream:
                layers.append(("udp", int(ustream), endpoint(net_s, usp), endpoint(net_d, udp_)))
        for kind, key, src, dst in layers:
            table = flows.convs[kind]
            conv = table.get(key)
            if conv is None:
                conv = table[key] = _Conv(a=src, b=dst, start=when, end=when, stream=key if kind in ("tcp", "udp") else None)
            conv.add(src == conv.a, size, when)
            if kind == "tcp" and tflags:
                bits = to_int(tflags) or 0
                conv.flags.update(name for bit, name in _TCP_FLAGS.items() if bits & bit)
            ends = flows.endpoints[kind]
            tx = ends.setdefault(src, _Endpoint())
            tx.tx_packets += 1
            tx.tx_bytes += size
            rx = ends.setdefault(dst, _Endpoint())
            rx.rx_packets += 1
            rx.rx_bytes += size
        if expert:
            number = to_int(values[-2]) or 0
            for text in every(values[-1]):
                match = _EXPERT.match(text)
                if not match:
                    continue
                severity, group, message = match.groups()
                key = (_severity(severity), group, message.replace("\\r\\n", "").strip())
                flows.expert[key] += 1
                flows.expert_first.setdefault(key, number)
    return flows


_CONV_SORT = {"bytes": lambda r: r["bytes"], "packets": lambda r: r["packets"], "duration": lambda r: r["duration"], "start": lambda r: -r["start"]}


def _conversation_rows(flows: Flows, kind: str, sort: str) -> list[dict[str, Any]]:
    rows = [conv.row(kind) for conv in flows.convs[kind].values()]
    rows.sort(key=_CONV_SORT[sort], reverse=True)
    return rows


def conversations(
    config: WiresharkConfig,
    file: str | os.PathLike[str],
    *,
    type: str = "tcp",
    filter: str | None = None,
    sort: str = "bytes",
    limit: int = 20,
) -> dict[str, Any]:
    """Who talked to whom: one row per conversation (``tcp``/``udp`` = a
    connection, with its stream index for ``follow``; ``ip``/``eth`` = a pair of
    hosts), with packets and bytes each way, start and duration, and for TCP
    which of SYN/FIN/RST were seen. Largest first."""
    if type not in CONVERSATION_TYPES:
        raise ValueError(f"type must be one of {', '.join(CONVERSATION_TYPES)}")
    if sort not in _CONV_SORT:
        raise ValueError(f"sort must be one of {', '.join(_CONV_SORT)}")
    rows = _conversation_rows(scan(config, file, filter=filter), type, sort)
    shown = rows[:limit] if limit else rows
    out: dict[str, Any] = {"file": os.fspath(file), "type": type}
    if filter:
        out["filter"] = filter
    return {**out, "count": len(shown), "total": len(rows), "truncated": len(shown) < len(rows), "conversations": shown}


def _endpoint_rows(flows: Flows, kind: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "address": address,
            "packets": e.tx_packets + e.rx_packets,
            "bytes": e.tx_bytes + e.rx_bytes,
            "tx": {"packets": e.tx_packets, "bytes": e.tx_bytes},
            "rx": {"packets": e.rx_packets, "bytes": e.rx_bytes},
        }
        for address, e in flows.endpoints[kind].items()
    ]
    rows.sort(key=lambda r: r["bytes"], reverse=True)
    return rows


def endpoints(config: WiresharkConfig, file: str | os.PathLike[str], *, type: str = "ip", filter: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Every address (``ip``/``eth``) or address:port (``tcp``/``udp``) seen,
    with packets and bytes sent and received. Busiest first."""
    if type not in CONVERSATION_TYPES:
        raise ValueError(f"type must be one of {', '.join(CONVERSATION_TYPES)}")
    rows = _endpoint_rows(scan(config, file, filter=filter), type)
    shown = rows[:limit] if limit else rows
    out: dict[str, Any] = {"file": os.fspath(file), "type": type}
    if filter:
        out["filter"] = filter
    return {**out, "count": len(shown), "total": len(rows), "truncated": len(shown) < len(rows), "endpoints": shown}


# ── expert info ─────────────────────────────────────────────────────────────


def _expert_rows(flows: Flows, floor: str) -> list[dict[str, Any]]:
    rank = {name: i for i, name in enumerate(SEVERITIES)}
    ranked = sorted(((rank.get(sev, 0), n, sev, group, message) for (sev, group, message), n in flows.expert.items() if rank.get(sev, 0) >= rank[floor]), key=lambda t: (-t[0], -t[1]))
    return [{"severity": sev, "group": group, "message": shorten(message, 200), "count": n, "first_packet": flows.expert_first[(sev, group, message)]} for _r, n, sev, group, message in ranked]


def expert(config: WiresharkConfig, file: str | os.PathLike[str], *, severity: str = "warning", filter: str | None = None, limit: int = 25) -> dict[str, Any]:
    """Wireshark's expert analysis — resets, retransmissions, zero windows,
    malformed packets, … — grouped by message, worst first, with a count and
    the first packet each was seen in. ``severity`` is the lowest level shown
    (``chat``, ``note``, ``warning``, ``error``)."""
    if severity not in SEVERITY_VALUE:
        raise ValueError(f"severity must be one of {', '.join(SEVERITY_VALUE)}")
    floor = f"_ws.expert.severity >= {SEVERITY_VALUE[severity]}"
    flows = scan(config, file, filter=f"({filter}) && {floor}" if filter else floor, expert=True)
    rows = _expert_rows(flows, severity)
    by_severity: Counter[str] = Counter()
    for row in rows:
        by_severity[row["severity"]] += row["count"]
    shown = rows[:limit] if limit else rows
    out: dict[str, Any] = {"file": os.fspath(file), "severity": severity}
    if filter:
        out["filter"] = filter
    return {**out, "by_severity": dict(by_severity), "count": len(shown), "total": len(rows), "truncated": len(shown) < len(rows), "messages": shown}


# ── traffic over time ──────────────────────────────────────────────────────

_NICE_INTERVALS = (0.001, 0.01, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400)


def pick_interval(duration: float, buckets: int) -> float:
    """The smallest round interval that fits ``duration`` into ``buckets``."""
    for step in _NICE_INTERVALS:
        if duration / step <= buckets:
            return float(step)
    return float(math.ceil(duration / buckets))


def io(
    config: WiresharkConfig,
    file: str | os.PathLike[str],
    *,
    interval: float | None = None,
    filter: str | None = None,
    limit: int = 60,
) -> dict[str, Any]:
    """Packets and bytes per time interval (gaps included), to see when
    traffic started, stopped, or spiked. Without ``interval`` one is chosen so
    the capture fits in ``limit`` buckets."""
    if interval is None:
        info = file_info(config, file)
        interval = pick_interval(float(info.get("duration_s") or 0.0), max(limit, 1))
    if interval <= 0:
        raise ValueError("interval must be positive")
    counts: dict[int, list[int]] = {}
    total_packets = total_bytes = 0
    for rel, length in iter_fields(config, file, ["frame.time_relative", "frame.len"], filter=filter, occurrence="f"):
        when = to_float(rel)
        if when is None:
            continue
        slot = counts.setdefault(int(when // interval), [0, 0])
        slot[0] += 1
        slot[1] += to_int(length) or 0
        total_packets += 1
        total_bytes += to_int(length) or 0
    last = max(counts) if counts else -1
    buckets = [{"start": round(i * interval, 6), "packets": counts.get(i, [0, 0])[0], "bytes": counts.get(i, [0, 0])[1]} for i in range(last + 1)]
    shown = buckets[:limit] if limit else buckets
    out: dict[str, Any] = {"file": os.fspath(file), "interval": interval}
    if filter:
        out["filter"] = filter
    out.update({"packets": total_packets, "bytes": total_bytes})
    if buckets:
        peak = max(buckets, key=lambda b: b["packets"])
        out["peak"] = {"start": peak["start"], "packets": peak["packets"], "bytes": peak["bytes"]}
        out["empty_buckets"] = sum(1 for b in buckets if not b["packets"])
    return {**out, "count": len(shown), "total": len(buckets), "truncated": len(shown) < len(buckets), "buckets": shown}


# ── raw -z ──────────────────────────────────────────────────────────────────


def stats(config: WiresharkConfig, file: str | os.PathLike[str], stat: str, *, max_lines: int = 100) -> dict[str, Any]:
    """Any tshark ``-z`` statistic (``http,tree``, ``dns,tree``, ``io,stat,1``,
    ``smb,srt``, ``rtp,streams`` …) as its own text lines, for what the other
    verbs don't cover. Capped at ``max_lines``."""
    name = stat.split(",", 1)[0].strip().lower()
    if name in REFUSED_STATS:
        raise ValueError(f"-z {name} prints credentials found in the capture; it is not offered here")
    if name in ("follow",):
        raise ValueError("use the follow verb for streams")
    text = run(read_argv(config, file) + ["-q", "-z", stat], timeout=config.timeout)
    lines = [line.rstrip() for line in text.splitlines() if line.strip() and not set(line.strip()) <= {"="}]
    shown = lines[:max_lines] if max_lines else lines
    return {"file": os.fspath(file), "stat": stat, "count": len(shown), "total": len(lines), "truncated": len(shown) < len(lines), "lines": shown}


# ── summary ─────────────────────────────────────────────────────────────────


def summary(config: WiresharkConfig, file: str | os.PathLike[str], *, filter: str | None = None, top: int = 5) -> dict[str, Any]:
    """A first look at a capture in one call: file info, the main protocols,
    the busiest hosts and connections, and expert warnings/errors — enough to
    pick the next verb."""
    out: dict[str, Any] = {"file": os.fspath(file)}
    if filter:
        out["filter"] = filter
    try:
        info = file_info(config, file)
    except WiresharkError:
        info = {}
    out["info"] = {k: v for k, v in info.items() if k in ("packets", "data_bytes", "duration_s", "start", "end", "encapsulation", "file_type")}
    proto = protocols(config, file, filter=filter)["protocols"]
    # The link and network layers are 100% by definition, and "tcp.segments"
    # style entries are reassembly bookkeeping; show the protocols above them.
    seen: set[str] = set()
    shown_protos = []
    for p in proto:
        if p["depth"] >= 2 and "." not in p["protocol"] and p["protocol"] not in seen:
            seen.add(p["protocol"])
            shown_protos.append({"protocol": p["protocol"], "frames": p["frames"], "percent": p["percent"]})
    out["protocols"] = shown_protos[: max(top * 2, 10)]
    flows = scan(config, file, filter=filter, expert=True)
    out["packets"] = flows.packets
    out["bytes"] = flows.bytes
    out["hosts"] = [{"address": r["address"], "packets": r["packets"], "bytes": r["bytes"]} for r in _endpoint_rows(flows, "ip")[:top]]
    keep = ("stream", "a", "b", "packets", "bytes", "duration", "flags")
    out["tcp"] = {"connections": len(flows.convs["tcp"]), "reset": sum(1 for c in flows.convs["tcp"].values() if "RST" in c.flags)}
    out["top_tcp"] = [{k: r[k] for k in keep if k in r} for r in _conversation_rows(flows, "tcp", "bytes")[:top]]
    out["udp"] = {"conversations": len(flows.convs["udp"])}
    out["top_udp"] = [{k: r[k] for k in keep if k in r} for r in _conversation_rows(flows, "udp", "bytes")[:top]]
    by_severity: Counter[str] = Counter()
    for (sev, _group, _message), n in flows.expert.items():
        by_severity[sev] += n
    out["expert"] = {s: by_severity[s] for s in reversed(SEVERITIES) if by_severity[s]}
    out["expert_top"] = _expert_rows(flows, "warning")[:top]
    return out

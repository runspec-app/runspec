"""packets.py — the packet list, one packet in detail, chosen fields, and the
most common values of a field."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any

from runspec_wireshark_core.client import (
    WiresharkConfig,
    WiresharkError,
    every,
    iter_fields,
    iter_lines,
    read_argv,
    run,
    shorten,
    to_float,
    to_int,
)
from runspec_wireshark_core.info import utc

MAX_INFO = 160
MAX_FIELDS = 20
MAX_LAYER_LINES = 40
_ROW_FIELDS = [
    "frame.number",
    "frame.time_relative",
    "frame.time_epoch",
    "_ws.col.Source",
    "_ws.col.Destination",
    "_ws.col.Protocol",
    "frame.len",
    "_ws.col.Info",
    "tcp.srcport",
    "tcp.dstport",
    "udp.srcport",
    "udp.dstport",
    "tcp.stream",
    "udp.stream",
]


def endpoint(address: str, port: str) -> str:
    """``addr:port`` (``[v6]:port``), or the bare address when there's no port."""
    if not port:
        return address
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def _page(total: int, offset: int, limit: int, count: int) -> dict[str, Any]:
    out: dict[str, Any] = {"count": count, "total": total, "offset": offset, "truncated": offset + count < total}
    if out["truncated"]:
        out["next_offset"] = offset + count
    return out


def packets(
    config: WiresharkConfig,
    file: str | os.PathLike[str],
    *,
    filter: str | None = None,
    limit: int = 25,
    offset: int = 0,
    absolute_time: bool = False,
) -> dict[str, Any]:
    """The packet list — number, time, source, destination, protocol, length,
    info, and the TCP/UDP stream index (for ``follow``) — for the packets
    matching ``filter``, a page at a time. ``total`` counts every match."""
    rows: list[dict[str, Any]] = []
    total = 0
    for values in iter_fields(config, file, _ROW_FIELDS, filter=filter, occurrence="f"):
        total += 1
        if total <= offset or (limit and len(rows) >= limit):
            continue
        number, rel, epoch, src, dst, proto, length, info, tsp, tdp, usp, udp_, tstream, ustream = values
        row: dict[str, Any] = {"no": to_int(number)}
        when = to_float(epoch if absolute_time else rel)
        if when is not None:
            row["time"] = utc(when) if absolute_time else round(when, 6)
        row["src"] = endpoint(src, tsp or usp)
        row["dst"] = endpoint(dst, tdp or udp_)
        row["proto"] = proto
        row["len"] = to_int(length)
        row["info"] = shorten(info.strip(), MAX_INFO)
        if tstream:
            row["stream"] = f"tcp {tstream}"
        elif ustream:
            row["stream"] = f"udp {ustream}"
        rows.append(row)
    out: dict[str, Any] = {"file": os.fspath(file)}
    if filter:
        out["filter"] = filter
    return {**out, **_page(total, offset, limit, len(rows)), "packets": rows}


# ── one packet ──────────────────────────────────────────────────────────────

# "0100 .... = Version: 4" → "Version: 4" (the bit picture is noise in text).
_BITS = re.compile(r"^[01.]{4}(?: [01.]{4})* = ")
_FRAME_KEEP = ("Arrival Time:", "Frame Length:", "Capture Length:", "[Protocols in frame:", "Interface id:", "[Time since reference")


def parse_verbose(text: str, *, depth: int = 1, max_lines: int = MAX_LAYER_LINES, full_frame: bool = False) -> list[dict[str, Any]]:
    """``tshark -V`` text for one packet → ``[{title, lines, more?}]``, one
    entry per protocol layer. Keeps lines up to ``depth`` levels under each
    layer title (indented two spaces per extra level) and at most
    ``max_lines`` per layer; the Frame layer keeps only its useful lines
    unless ``full_frame``."""
    layers: list[dict[str, Any]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            layers.append({"title": raw.strip(), "lines": [], "more": 0})
            continue
        if not layers:
            continue
        level = indent // 4
        if level > depth:
            continue
        line = _BITS.sub("", raw.strip())
        layer = layers[-1]
        if layer["title"].startswith("Frame ") and not full_frame and not (level == 1 and line.startswith(_FRAME_KEEP)):
            continue
        if len(layer["lines"]) >= max_lines:
            layer["more"] += 1
            continue
        layer["lines"].append("  " * (level - 1) + line)
    for layer in layers:
        if not layer["more"]:
            del layer["more"]
        if not layer["lines"]:
            del layer["lines"]
    return layers


def _hex_dump(text: str, max_lines: int) -> dict[str, Any]:
    lines = [line for line in text.splitlines() if re.match(r"^[0-9a-f]{4,}  ", line)]
    out: dict[str, Any] = {"bytes": lines[:max_lines]}
    if len(lines) > max_lines:
        out["bytes_more_lines"] = len(lines) - max_lines
    return out


def packet(
    config: WiresharkConfig,
    file: str | os.PathLike[str],
    number: int,
    *,
    layers: list[str] | None = None,
    depth: int = 1,
    include: list[str] | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    """One packet, decoded: each protocol layer's title and its fields, to
    ``depth`` levels. ``layers`` (``ip``, ``tcp``, ``http`` …) limits the detail
    to those protocols; ``include`` adds ``bytes`` (a hex dump) and ``frame``
    (the whole Frame layer); ``raw`` returns tshark's JSON for the packet."""
    include = include or []
    flt = f"frame.number == {int(number)}"
    if raw:
        argv = read_argv(config, file, filter=flt) + ["-T", "json", "--no-duplicate-keys"]
        if layers:
            argv += ["-j", " ".join(layers)]
        data = json.loads(run(argv, timeout=config.timeout) or "[]")
        if not data:
            raise WiresharkError(f"no packet {number} in {os.fspath(file)}")
        return data[0].get("_source", {}).get("layers", {})
    argv = read_argv(config, file, filter=flt) + ["-V"]
    if layers:
        argv += ["-O", ",".join(layers)]
    if "bytes" in include:
        argv.append("-x")
    text = "\n".join(iter_lines(argv, timeout=config.timeout))
    detail, _, dump = text.partition("\n\n")
    parsed = parse_verbose(detail, depth=depth, full_frame="frame" in include)
    if not parsed:
        raise WiresharkError(f"no packet {number} in {os.fspath(file)}")
    out: dict[str, Any] = {"no": int(number), "layers": parsed}
    if "bytes" in include:
        out.update(_hex_dump(dump or text, 64))
    return out


# ── fields / top ────────────────────────────────────────────────────────────


def _value(raw: str, max_chars: int) -> Any:
    values = every(raw)
    if not values:
        return None
    if len(values) == 1:
        return shorten(values[0], max_chars)
    return [shorten(v, max_chars) for v in values]


def fields(
    config: WiresharkConfig,
    file: str | os.PathLike[str],
    names: list[str],
    *,
    filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
    max_chars: int = 200,
) -> dict[str, Any]:
    """Chosen fields (``ip.src``, ``http.host``, ``dns.qry.name`` …) for each
    matching packet, a page at a time. A field that repeats in a packet comes
    back as a list; an absent one is left out of that row."""
    if not names:
        raise ValueError("name at least one field (e.g. --field ip.src --field http.host)")
    if len(names) > MAX_FIELDS:
        raise ValueError(f"at most {MAX_FIELDS} fields per call")
    rows: list[dict[str, Any]] = []
    total = 0
    for values in iter_fields(config, file, ["frame.number", *names], filter=filter):
        total += 1
        if total <= offset or (limit and len(rows) >= limit):
            continue
        row: dict[str, Any] = {"no": to_int(values[0])}
        for name, raw in zip(names, values[1:], strict=False):
            value = _value(raw, max_chars)
            if value is not None:
                row[name] = value
        rows.append(row)
    out: dict[str, Any] = {"file": os.fspath(file), "fields": names}
    if filter:
        out["filter"] = filter
    return {**out, **_page(total, offset, limit, len(rows)), "rows": rows}


def top(
    config: WiresharkConfig,
    file: str | os.PathLike[str],
    field: str,
    *,
    filter: str | None = None,
    by: str = "packets",
    limit: int = 10,
) -> dict[str, Any]:
    """The most common values of one field (``ip.src``, ``http.host``,
    ``tls.handshake.extensions_server_name`` …), counted by packets or summed
    by bytes. Packets without the field count as ``missing``."""
    if by not in ("packets", "bytes"):
        raise ValueError("by must be packets or bytes")
    counts: Counter[str] = Counter()
    packets_seen = missing = 0
    for raw, length in iter_fields(config, file, [field, "frame.len"], filter=filter):
        packets_seen += 1
        values = set(every(raw))
        if not values:
            missing += 1
            continue
        weight = (to_int(length) or 0) if by == "bytes" else 1
        for value in values:
            counts[value] += weight
    grand = sum(counts.values()) or 1
    ranked = counts.most_common(limit or None)
    out: dict[str, Any] = {"file": os.fspath(file), "field": field, "by": by}
    if filter:
        out["filter"] = filter
    out.update({"packets": packets_seen, "missing": missing, "distinct": len(counts), "truncated": len(counts) > len(ranked)})
    out["values"] = [{"value": shorten(value, 200), by: n, "percent": round(100 * n / grand, 1)} for value, n in ranked]
    return out


__all__ = ["endpoint", "fields", "packet", "packets", "parse_verbose", "top"]

"""apps.py — the application-level questions asked most in support work: which
DNS lookups failed, which HTTP requests errored or were slow, and which TLS
handshakes failed and why."""

from __future__ import annotations

import os
from collections import Counter
from typing import Any

from runspec_wireshark_core.client import WiresharkConfig, every, first, iter_fields, shorten, to_float, to_int
from runspec_wireshark_core.packets import endpoint

DNS_RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED", 6: "YXDOMAIN", 7: "YXRRSET", 8: "NXRRSET", 9: "NOTAUTH", 10: "NOTZONE"}
DNS_TYPES = {1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX", 16: "TXT", 28: "AAAA", 33: "SRV", 35: "NAPTR", 43: "DS", 48: "DNSKEY", 64: "SVCB", 65: "HTTPS", 255: "ANY", 257: "CAA"}
TLS_VERSIONS = {0x0300: "SSL 3.0", 0x0301: "TLS 1.0", 0x0302: "TLS 1.1", 0x0303: "TLS 1.2", 0x0304: "TLS 1.3"}
# RFC 8446 §6 alert descriptions.
TLS_ALERTS = {
    0: "close_notify",
    10: "unexpected_message",
    20: "bad_record_mac",
    22: "record_overflow",
    40: "handshake_failure",
    42: "bad_certificate",
    43: "unsupported_certificate",
    44: "certificate_revoked",
    45: "certificate_expired",
    46: "certificate_unknown",
    47: "illegal_parameter",
    48: "unknown_ca",
    49: "access_denied",
    50: "decode_error",
    51: "decrypt_error",
    70: "protocol_version",
    71: "insufficient_security",
    80: "internal_error",
    86: "inappropriate_fallback",
    90: "user_canceled",
    109: "missing_extension",
    110: "unsupported_extension",
    112: "unrecognized_name",
    113: "bad_certificate_status_response",
    115: "unknown_psk_identity",
    116: "certificate_required",
    120: "no_application_protocol",
}
# The cipher suites seen in practice; anything else is shown as its hex code.
TLS_CIPHERS = {
    0x1301: "TLS_AES_128_GCM_SHA256",
    0x1302: "TLS_AES_256_GCM_SHA384",
    0x1303: "TLS_CHACHA20_POLY1305_SHA256",
    0xC02B: "ECDHE-ECDSA-AES128-GCM-SHA256",
    0xC02C: "ECDHE-ECDSA-AES256-GCM-SHA384",
    0xC02F: "ECDHE-RSA-AES128-GCM-SHA256",
    0xC030: "ECDHE-RSA-AES256-GCM-SHA384",
    0xCCA8: "ECDHE-RSA-CHACHA20-POLY1305",
    0xCCA9: "ECDHE-ECDSA-CHACHA20-POLY1305",
    0xC013: "ECDHE-RSA-AES128-SHA",
    0xC014: "ECDHE-RSA-AES256-SHA",
    0xC023: "ECDHE-ECDSA-AES128-SHA256",
    0xC024: "ECDHE-ECDSA-AES256-SHA384",
    0xC027: "ECDHE-RSA-AES128-SHA256",
    0xC028: "ECDHE-RSA-AES256-SHA384",
    0x009C: "AES128-GCM-SHA256",
    0x009D: "AES256-GCM-SHA384",
    0x009E: "DHE-RSA-AES128-GCM-SHA256",
    0x009F: "DHE-RSA-AES256-GCM-SHA384",
    0x002F: "AES128-SHA",
    0x0035: "AES256-SHA",
    0x003C: "AES128-SHA256",
    0x003D: "AES256-SHA256",
    0x000A: "DES-CBC3-SHA",
}


def _name(table: dict[int, str], raw: str) -> str | None:
    code = to_int(raw)
    if code is None:
        return None
    return table.get(code, f"0x{code:04x}" if table is TLS_CIPHERS or table is TLS_VERSIONS else str(code))


def _head(file: str | os.PathLike[str], filter: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"file": os.fspath(file)}
    if filter:
        out["filter"] = filter
    return out


def _and(base: str, extra: str | None) -> str:
    return f"({base}) && ({extra})" if extra else base


# ── DNS ─────────────────────────────────────────────────────────────────────

_DNS_FIELDS = ["frame.number", "dns.id", "dns.flags.response", "dns.qry.name", "dns.qry.type", "dns.flags.rcode", "dns.time", "dns.a", "dns.aaaa", "dns.cname", "ip.src", "ipv6.src"]


def dns(config: WiresharkConfig, file: str | os.PathLike[str], *, filter: str | None = None, failed: bool = False, limit: int = 25) -> dict[str, Any]:
    """DNS lookups grouped by name and type: how often asked, how often
    answered, the response codes (NXDOMAIN, SERVFAIL …), the slowest response,
    and a few answers. Failures sort first; ``failed`` shows only them."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    rcodes_all: Counter[str] = Counter()
    unanswered = 0
    for number, _qid, response, qname, qtype, rcode, rtt, a, aaaa, cname, src, src6 in iter_fields(config, file, _DNS_FIELDS, filter=_and("dns", filter)):
        name, kind = first(qname), _name(DNS_TYPES, qtype) or "?"
        if not name:
            continue
        row = groups.setdefault(
            (name, kind), {"name": name, "type": kind, "queries": 0, "responses": 0, "rcodes": Counter(), "answers": [], "max_ms": None, "first_packet": to_int(number), "clients": set()}
        )
        if response.lower() in ("true", "1"):
            row["responses"] += 1
            code = _name(DNS_RCODES, rcode) or "NOERROR"
            row["rcodes"][code] += 1
            rcodes_all[code] += 1
            when = to_float(rtt)
            if when is not None:
                ms = round(when * 1000, 3)
                row["max_ms"] = ms if row["max_ms"] is None else max(row["max_ms"], ms)
            for answer in [*every(a), *every(aaaa), *every(cname)]:
                if answer not in row["answers"] and len(row["answers"]) < 5:
                    row["answers"].append(answer)
        else:
            row["queries"] += 1
            row["clients"].add(first(src) or first(src6))
    rows = []
    for row in groups.values():
        missing = max(row["queries"] - row["responses"], 0)
        unanswered += missing
        bad = {k: v for k, v in row["rcodes"].items() if k != "NOERROR"}
        out_row: dict[str, Any] = {"name": row["name"], "type": row["type"], "queries": row["queries"], "responses": row["responses"]}
        if row["rcodes"]:
            out_row["rcodes"] = dict(row["rcodes"])
        if missing:
            out_row["unanswered"] = missing
        if row["max_ms"] is not None:
            out_row["max_ms"] = row["max_ms"]
        if row["answers"]:
            out_row["answers"] = row["answers"]
        out_row["clients"] = len(row["clients"] - {""})
        out_row["first_packet"] = row["first_packet"]
        out_row["_failed"] = bool(bad) or bool(missing)
        rows.append(out_row)
    if failed:
        rows = [r for r in rows if r["_failed"]]
    rows.sort(key=lambda r: (not r["_failed"], -(r["queries"] or r["responses"])))
    for r in rows:
        del r["_failed"]
    shown = rows[:limit] if limit else rows
    out = _head(file, filter)
    out.update({"rcodes": dict(rcodes_all), "unanswered": unanswered})
    return {**out, "count": len(shown), "total": len(rows), "truncated": len(shown) < len(rows), "lookups": shown}


# ── HTTP ────────────────────────────────────────────────────────────────────

_HTTP_FIELDS = [
    "frame.number",
    "frame.time_relative",
    "tcp.stream",
    "http.request.method",
    "http.host",
    "http.request.uri",
    "http.response.code",
    "http.response.phrase",
    "http.time",
    "http.request_in",
    "http.content_type",
    "http.content_length",
    "http.user_agent",
    "http.server",
    "ip.src",
    "ipv6.src",
    "ip.dst",
    "ipv6.dst",
    "tcp.srcport",
    "tcp.dstport",
]


def http(
    config: WiresharkConfig,
    file: str | os.PathLike[str],
    *,
    filter: str | None = None,
    errors: bool = False,
    slow_ms: float | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """HTTP/1.x requests with their responses: method, host, URI, status, and
    response time, plus a count by status. ``errors`` keeps only 4xx/5xx and
    unanswered requests; ``slow_ms`` keeps only responses slower than that.
    (HTTP/2 and HTTPS need ``--keylog-file`` to be readable at all.)"""
    requests: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    statuses: Counter[str] = Counter()
    for values in iter_fields(config, file, _HTTP_FIELDS, filter=_and("http.request || http.response", filter)):
        number, rel, stream, method, host, uri, code, phrase, rtt, req_in, ctype, clen, agent, server, src, src6, dst, dst6, sport, dport = values
        frame = to_int(number) or 0
        if first(method):
            requests[frame] = {
                "no": frame,
                "time": round(to_float(rel) or 0.0, 6),
                "stream": to_int(stream),
                "client": endpoint(first(src) or first(src6), first(sport)),
                "server": endpoint(first(dst) or first(dst6), first(dport)),
                "method": first(method),
                "host": first(host),
                "uri": shorten(first(uri), 200),
            }
            if first(agent):
                requests[frame]["user_agent"] = shorten(first(agent), 80)
            order.append(frame)
        if first(code):
            status = first(code)
            statuses[status] += 1
            req = requests.get(to_int(req_in) or -1)
            if req is None:
                continue
            req["status"] = int(status)
            if first(phrase):
                req["reason"] = first(phrase)
            when = to_float(rtt)
            if when is not None:
                req["ms"] = round(when * 1000, 3)
            if first(ctype):
                req["content_type"] = first(ctype)
            if to_int(clen) is not None:
                req["length"] = to_int(clen)
            if first(server).strip():
                req["server_header"] = first(server).strip()
            req["response_no"] = frame
    rows = [requests[n] for n in order]
    unanswered = sum(1 for r in rows if "status" not in r)
    if errors:
        rows = [r for r in rows if r.get("status", 0) >= 400 or "status" not in r]
    if slow_ms is not None:
        rows = [r for r in rows if r.get("ms", 0) > slow_ms]
    shown = rows[:limit] if limit else rows
    out = _head(file, filter)
    out.update({"requests": len(order), "unanswered": unanswered, "by_status": dict(sorted(statuses.items()))})
    return {**out, "count": len(shown), "total": len(rows), "truncated": len(shown) < len(rows), "rows": shown}


# ── TLS ─────────────────────────────────────────────────────────────────────

_TLS_FIELDS = [
    "frame.number",
    "tcp.stream",
    "tls.handshake.type",
    "tls.handshake.extensions_server_name",
    "tls.handshake.version",
    "tls.handshake.extensions.supported_version",
    "tls.handshake.ciphersuite",
    "tls.handshake.extensions_alpn_str",
    "tls.alert_message.level",
    "tls.alert_message.desc",
    "ip.src",
    "ipv6.src",
    "ip.dst",
    "ipv6.dst",
    "tcp.srcport",
    "tcp.dstport",
    "x509sat.printableString",
    "x509sat.uTF8String",
]


def tls(config: WiresharkConfig, file: str | os.PathLike[str], *, filter: str | None = None, failed: bool = False, limit: int = 25) -> dict[str, Any]:
    """TLS handshakes, one row per connection: server name (SNI), the version
    and cipher agreed, ALPN, certificate names when visible (TLS 1.2 and
    older), and any alert — ``unknown_ca``, ``handshake_failure``,
    ``certificate_expired`` … Failed handshakes sort first; ``failed`` shows
    only them."""
    conns: dict[int, dict[str, Any]] = {}
    alerts: Counter[str] = Counter()
    flt = _and("tls.handshake.type == 1 || tls.handshake.type == 2 || tls.handshake.type == 11 || tls.alert_message", filter)
    for values in iter_fields(config, file, _TLS_FIELDS, filter=flt):
        number, stream, htype, sni, hversion, supported, cipher, alpn, level, desc, src, src6, dst, dst6, sport, dport, printable, utf8 = values
        key = to_int(stream)
        if key is None:
            continue
        types = {to_int(t) for t in every(htype)}
        conn = conns.get(key)
        if conn is None:
            conn = conns[key] = {
                "stream": key,
                "client": endpoint(first(src) or first(src6), first(sport)),
                "server": endpoint(first(dst) or first(dst6), first(dport)),
                "first_packet": to_int(number),
            }
        if 1 in types:
            if first(sni):
                conn["sni"] = first(sni)
            offered = [TLS_VERSIONS.get(to_int(v) or 0, v) for v in every(supported)] or [_name(TLS_VERSIONS, hversion) or ""]
            conn["offered"] = [v for v in offered if v]
            if first(alpn):
                conn["alpn_offered"] = every(alpn)
        if 2 in types:
            # A TLS 1.3 ServerHello says 1.2 in its header and the real version
            # in supported_versions.
            conn["version"] = _name(TLS_VERSIONS, first(supported) or first(hversion))
            conn["cipher"] = _name(TLS_CIPHERS, cipher)
            if first(alpn):
                conn["alpn"] = first(alpn)
        if 11 in types:
            names = list(dict.fromkeys([*every(printable), *every(utf8)]))
            if names:
                conn["cert_names"] = names[:6]
        if first(desc) or first(level):
            # An encrypted alert (TLS 1.3, or after the handshake) has no
            # readable level/description.
            name = _name(TLS_ALERTS, desc) or "encrypted"
            fatal = first(level) == "2"
            entry = {"from": endpoint(first(src) or first(src6), first(sport)), "alert": name, "fatal": fatal, "no": to_int(number)}
            conn.setdefault("alerts", []).append(entry)
            if name not in ("close_notify", "encrypted"):
                alerts[name] += 1
    rows = list(conns.values())
    for row in rows:
        bad = [a for a in row.get("alerts", []) if a["alert"] not in ("close_notify", "encrypted")]
        row["ok"] = "version" in row and not any(a["fatal"] for a in bad)
    if failed:
        rows = [r for r in rows if not r["ok"]]
    rows.sort(key=lambda r: (r["ok"], r["first_packet"] or 0))
    shown = rows[:limit] if limit else rows
    out = _head(file, filter)
    out.update({"handshakes": len(conns), "failed": sum(1 for r in conns.values() if not r["ok"]), "alerts": dict(alerts)})
    return {**out, "count": len(shown), "total": len(rows), "truncated": len(shown) < len(rows), "connections": shown}

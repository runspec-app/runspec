"""follow.py — reassemble one stream's payload, like Wireshark's Follow Stream."""

from __future__ import annotations

import os
import re
from typing import Any

from runspec_wireshark_core.client import WiresharkConfig, read_argv, run

FOLLOW_PROTOCOLS = ("tcp", "udp", "tls", "http", "http2", "quic", "sip", "websocket", "dccp")
DEFAULT_MAX_BYTES = 4000


_HEADER = re.compile(r"(\t?)(\d+)\r?\n")
_FOOTER = "=" * 20


def parse_follow(text: str) -> dict[str, Any]:
    """``-z follow,<proto>,ascii,<n>`` text → nodes + chunks.

    tshark prints ``Node 0:`` / ``Node 1:`` addresses, then each chunk as a
    line holding its byte count followed by exactly that many characters of
    payload (``.`` for non-printable bytes) and a newline; node 1's count line
    starts with a tab. Returns ``{"nodes": [a, b], "chunks": [{"from": addr,
    "bytes": n, "text": …}]}``.
    """
    nodes: list[str] = []
    pos = 0
    for match in re.finditer(r"^Node (\d+): (.*?)\r?$", text, re.MULTILINE):
        nodes.append(match.group(2).strip())
        pos = match.end() + 1
        if len(nodes) == 2:  # later "Node n:" lines would be payload
            break
    chunks: list[dict[str, Any]] = []
    if not nodes:
        return {"nodes": nodes, "chunks": chunks}
    while pos < len(text):
        if text.startswith(_FOOTER, pos):
            break
        head = _HEADER.match(text, pos)
        if not head:
            # Unexpected shape: skip to the next line and keep looking.
            nxt = text.find("\n", pos)
            if nxt < 0:
                break
            pos = nxt + 1
            continue
        size = int(head.group(2))
        body_start = head.end()
        body = text[body_start : body_start + size]
        pos = body_start + size
        if text.startswith("\r\n", pos):
            pos += 2
        elif text.startswith("\n", pos):
            pos += 1
        node = 1 if head.group(1) else 0
        chunks.append({"from": nodes[node] if node < len(nodes) else str(node), "bytes": size, "text": body})
    return {"nodes": nodes, "chunks": chunks}


def follow(
    config: WiresharkConfig,
    file: str | os.PathLike[str],
    *,
    protocol: str = "tcp",
    stream: int = 0,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """The payload of one TCP/UDP/TLS/HTTP/… stream as text, in order, with who
    sent each part — the stream index comes from ``packets`` or
    ``conversations``. Non-printable bytes show as ``.``; TLS needs
    ``--keylog-file``. Text past ``max_bytes`` (0 = all) is cut and
    ``truncated`` set."""
    if protocol not in FOLLOW_PROTOCOLS:
        raise ValueError(f"protocol must be one of {', '.join(FOLLOW_PROTOCOLS)}")
    text = run(read_argv(config, file) + ["-q", "-z", f"follow,{protocol},ascii,{int(stream)}"], timeout=config.timeout)
    parsed = parse_follow(text)
    # tshark prints "Node 0: :0" when it found nothing to follow.
    nodes = [n for n in parsed["nodes"] if n and not n.startswith(":")]
    chunks = parsed["chunks"]
    total = sum(c["bytes"] for c in chunks)
    budget = max_bytes if max_bytes > 0 else None
    shown: list[dict[str, Any]] = []
    used = 0
    for chunk in chunks:
        if budget is not None and used >= budget:
            break
        piece = chunk["text"]
        if budget is not None and used + len(piece) > budget:
            piece = piece[: budget - used]
        used += len(piece)
        shown.append({"from": chunk["from"], "bytes": chunk["bytes"], "text": piece})
    out: dict[str, Any] = {"file": os.fspath(file), "protocol": protocol, "stream": int(stream)}
    if len(nodes) == 2:
        out["a"], out["b"] = nodes
    cut = len(shown) < len(chunks) or any(len(s["text"]) < len(c["text"]) for s, c in zip(shown, chunks, strict=False))
    out.update({"chunks_total": len(chunks), "bytes_total": total, "truncated": cut})
    if not chunks:
        hint = f"no {protocol} payload for stream {stream} — check the index with the conversations verb"
        if protocol == "tls" and not config.keylog_file:
            hint += "; TLS needs --keylog-file with the session keys (SSLKEYLOGFILE format) to decrypt"
        out["hint"] = hint
    out["chunks"] = shown
    return out

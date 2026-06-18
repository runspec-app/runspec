"""Pure, I/O-free line normalisation and event-signature extraction.

This is what makes log condensing cheap: collapse the *variable* parts of a log
line (timestamps, ids, addresses, numbers, quoted literals) into a stable
"shape" so that thousands of near-identical lines fold onto one signature. For a
multi-line stack trace, the signature is the exception class plus the top normalised
frames, so the same failure clusters regardless of the surrounding noise.

Nothing here reads files or the clock — every function is a deterministic pure
transform, which is what the unit tests pin.
"""

from __future__ import annotations

import re

# --- pieces of a line that vary run-to-run and must be masked for clustering ---
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_LONGHEX = re.compile(r"\b[0-9a-fA-F]{16,}\b")
# A hex-ish id (request id, short hash): >=4 chars, mixing at least one digit and
# one a-f letter — masks ``7f3a``/``0a1b`` while leaving plain words alone.
_HEXID = re.compile(r"\b(?=[0-9a-fA-F]*[0-9])(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{4,}\b")
_QUOTED = re.compile(r"""(['"]).*?\1""")
_NUM = re.compile(r"\d+")  # any digit run, incl. unit-glued (412ms) — masked last
_WS = re.compile(r"\s+")

# A leading timestamp: ISO (``2026-06-18T08:01:11`` / ``... 08:01:11,123``),
# bracketed (``[2026-06-18 08:01:11]``), or syslog (``Jun 18 08:01:11``).
_LEADING_TS = re.compile(
    r"""^\s*
    (?:
        \[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?
      | [A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}
    )
    \s*""",
    re.VERBOSE,
)

# Common severity tokens, mapped to an ordered rank.
_LEVELS = {
    "DEBUG": 0,
    "TRACE": 0,
    "INFO": 1,
    "NOTICE": 1,
    "WARN": 2,
    "WARNING": 2,
    "ERROR": 3,
    "ERR": 3,
    "SEVERE": 3,
    "CRITICAL": 4,
    "CRIT": 4,
    "FATAL": 4,
}
_LEVEL_RANK = {"all": 0, "debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}
_LEVEL_TOKEN = re.compile(r"\b(DEBUG|TRACE|INFO|NOTICE|WARN(?:ING)?|ERR(?:OR)?|SEVERE|CRIT(?:ICAL)?|FATAL)\b")

# An exception/error class name, e.g. ``NullPointerException`` / ``ValueError``.
_EXC = re.compile(r"\b([A-Za-z_][\w.]*(?:Error|Exception|Failure|Fault))\b")

# Stack-frame patterns across common runtimes.
_FRAME_JAVA = re.compile(r"\bat\s+([\w.$]+)\s*\(")  # at com.acme.Foo.bar(Foo.java:42)
_FRAME_PY = re.compile(r'File\s+"[^"]+",\s+line\s+\d+,\s+in\s+(\S+)')  # File "x.py", line 5, in foo
_FRAME_JS = re.compile(r"\bat\s+([\w.$<>]+)\s*\(")  # at Object.fn (/a/b.js:1:2)


def strip_timestamp(line: str) -> str:
    """Remove a leading timestamp prefix from ``line`` (no-op when absent)."""
    return _LEADING_TS.sub("", line, count=1)


def detect_level(line: str) -> str | None:
    """Return the canonical severity name found in ``line`` (e.g. ``"error"``), or None."""
    m = _LEVEL_TOKEN.search(line)
    if not m:
        return None
    token = m.group(1).upper()
    rank = _LEVELS[token]
    # Map the rank back to a canonical name used by the level filter.
    for name, r in _LEVEL_RANK.items():
        if name != "all" and r == rank:
            return name
    return None


def normalize_line(line: str) -> str:
    """Collapse the variable parts of a single log line into a stable shape.

    Masks (in order) the leading timestamp, UUIDs, hex addresses, long hex
    blobs, quoted literals and bare integers, then squeezes whitespace. Two lines
    that differ only in those volatile parts return the same string.
    """
    s = strip_timestamp(line)
    s = _UUID.sub("<uuid>", s)
    s = _HEX.sub("<hex>", s)
    s = _LONGHEX.sub("<hex>", s)
    s = _HEXID.sub("<hex>", s)
    s = _QUOTED.sub("<str>", s)
    s = _NUM.sub("<n>", s)
    return _WS.sub(" ", s).strip()


def _frames(block: list[str], max_frames: int = 3) -> list[str]:
    """Extract up to ``max_frames`` normalised stack frames from an event block."""
    out: list[str] = []
    for raw in block:
        for pat in (_FRAME_PY, _FRAME_JAVA, _FRAME_JS):
            m = pat.search(raw)
            if m:
                out.append(m.group(1))
                break
        if len(out) >= max_frames:
            break
    return out


def error_signature(block: list[str], max_frames: int = 3) -> str:
    """Return a short, stable signature for an event (one or more lines).

    For a stack trace: ``<ExceptionClass>@<frame1>>major<frame2>...`` using the
    first exception class seen and the top normalised frames — so the same crash
    clusters regardless of message text or addresses. For a plain line with no
    exception/frames, falls back to the normalised first line (truncated).
    """
    if not block:
        return ""
    exc: str | None = None
    for raw in block:
        m = _EXC.search(raw)
        if m:
            exc = m.group(1).rsplit(".", 1)[-1]  # bare class name
            break
    frames = _frames(block, max_frames=max_frames)
    if exc or frames:
        head = exc or "error"
        return head + "@" + ">".join(frames) if frames else head
    # Plain line — normalised shape is the signature.
    return normalize_line(block[0])[:200]

"""
workspaces.py — per-session agent sandboxes for large tool output.

Each chat conversation (and each headless trigger/schedule turn) gets a workspace
directory at ``config.workspace_dir(session_id)``. When a runnable's stdout
exceeds ``_TOOL_OUTPUT_SPILL_THRESHOLD`` (see ``bridge_const``) it is written to
``tool-results/{tool_name}-{inv_id}.{json|txt}`` in that workspace instead of
inlined; the model gets a short envelope with a head preview + the on-disk path
and reaches for the ``workspace_*`` tools to interrogate the rest.

This module holds **pure helpers only** — no ``self``, no Bridge state, so every
function is unit-testable on any OS. The tool handlers in
``bridge_agent_tools.py`` are thin wrappers.

Design invariants
=================

* **Path-jailed.** Every read/write resolves the workspace-relative path
  under the session root and rejects any escape (``..``, absolute paths,
  symlinks that leave the tree). Same idiom as ``repos._safe_path``.
* **Machine-local.** Workspaces are ``never`` synced — the same class as
  ``conversations/``. Retention drops them alongside the conversation they
  belong to (chat) or after ``[workspace] headless_retention_days`` (headless).
* **The system owns ``tool-results/``.** ``workspace_write`` refuses to write
  into it — that dir carries provenance for what a runnable actually emitted,
  and the model shouldn't be able to overwrite that record.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

# Directory names inside a workspace.
TOOL_RESULTS_DIRNAME = "tool-results"  # system-written (spilled tool output)
NOTES_DIRNAME = "notes"  # agent-authored

# Caps mirror repos.py so a pathological file can't blow the model context.
_READ_MAX_BYTES = 512_000  # a single workspace_read's content is capped at ~512 KB
_READ_MAX_LINES = 2_000  # …and 2000 lines
_READ_SCAN_MAX_BYTES = 64_000_000  # bound the per-call line scan for a huge file
_GREP_FILE_MAX_BYTES = 5_000_000
_GREP_MAX_RESULTS = 200
_HEAD_TAIL_MAX_LINES = 500
_LS_MAX_ENTRIES = 500

# Dirs skipped by grep — the workspace is meant to be shallow (tool-results/,
# notes/), but a runnable that dumps a nested source tree still shouldn't drag
# vendored junk into the scan.
_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__", ".mypy_cache"})


def _safe_id(session_id: str) -> str:
    """Keep only filename-safe chars — session ids are hex/short slugs by
    contract (a conversation ``uuid4().hex[:16]``, a ``trigger-<event>``, or a
    ``schedule-<run>``), but never trust an id straight into a path. Empty
    string on rejection, so the caller falls back to *no workspace*."""
    return "".join(c for c in str(session_id) if c.isalnum() or c in "-_")


def _sanitize_filename_part(raw: str) -> str:
    """Squash a tool name (which can contain ``__`` / ``:`` / etc.) down to
    filename-safe chars for use inside a spill filename."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", str(raw or "tool"))[:60] or "tool"


def _is_binary(data: bytes) -> bool:
    """Cheap null-byte heuristic — good enough to skip binaries in read/grep."""
    return b"\x00" in data[:4096]


def _split_lines(text: str) -> list[str]:
    """Split ``text`` on newlines only, normalising CRLF.

    Unlike ``str.splitlines()`` — which also breaks on ``\\v \\f \\x1c-\\x1e
    \\x85 \\u2028 \\u2029`` — this splits on ``\\n`` alone and strips a trailing
    ``\\r`` per line, so line counts and grep line numbers match ``\\n``-based
    tools (and ``read_file``, which iterates the file with ``newline=""``). A form
    feed or NEL inside a spilled log/JSON value no longer invents a line."""
    if not text:
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # a trailing newline shouldn't yield a phantom empty line
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    """Write ``data`` to ``dest`` atomically (temp file + ``os.replace``), so a
    disk-full/interrupted write can never leave a truncated file masquerading as a
    complete ``tool-results/`` provenance record. On failure the temp file is
    cleaned up and the error propagates (the caller falls back)."""
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _is_json_line(text: str) -> bool:
    """The last non-empty stripped line looks like a JSON object/array —
    lets us pick ``.json`` vs ``.txt`` for the spilled file (so ``workspace_read``
    on a JSON dump renders sensibly in the UI hint and the agent knows the shape
    without inspecting the header)."""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if not (stripped.startswith(("{", "[")) and stripped.endswith(("}", "]"))):
            return False
        try:
            json.loads(stripped)
            return True
        except (ValueError, json.JSONDecodeError):
            return False
    return False


# ── JSON shape preview (envelope legibility for spilled JSON) ──────────────────
#
# A spilled JSON dump is often *minified* (one giant line), so the raw head-byte
# slice the envelope used to show is a mangled, near-useless prefix. Instead, when
# the output parses as JSON we hand the model a compact **shape** summary — the
# top-level type, an object's keys→types, an array's length + the field set of its
# records — so it knows exactly how to interrogate the file (which fields to grep,
# whether to reach for run_sql/run_python) without a wasted read.

_JSON_PREVIEW_MAX_CHARS = 25_000_000  # don't attempt to parse beyond this
_JSON_SHAPE_MAX_KEYS = 40  # cap keys listed per object
_JSON_SHAPE_MAX_DEPTH = 2  # how deep to describe nested structure
_JSON_ARRAY_KEY_SAMPLE = 20  # elements sampled to union an array's record keys
# A string field at least this long is shown in the shape summary with its size
# (``body: str[41 KB]``) instead of a bare ``str`` — the one JSON shape that a
# minified dump hides, and the field an agent most wants to know is large before
# pulling it (see the ``extract`` delivery mode).
_LARGE_STR_BYTES = 8_192


def human_size(n: int) -> str:
    """Human-readable byte/char count: ``B`` under 1 KB, ``KB`` (integer) under
    1 MB, ``MB`` (one decimal) above. Used in shape summaries and delivery notes."""
    n = int(n)
    if n < 1024:
        return f"{n} B"
    if n < 1_048_576:
        return f"{round(n / 1024)} KB"
    return f"{n / 1_048_576:.1f} MB"


def _json_type_name(value: Any) -> str:
    """Compact type label for a parsed JSON value (bool before int — ``bool`` is
    an ``int`` subclass). A long string carries its size (``str[41 KB]``) so the
    shape summary reveals which field dominates a document."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return (
            f"str[{human_size(len(value))}]" if len(value) > _LARGE_STR_BYTES else "str"
        )
    if value is None:
        return "null"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _json_shape(value: Any, depth: int = 0) -> str:
    """Render a compact, bounded shape descriptor for a parsed JSON value.

    Objects show ``{key: type, …}`` (capped, ``+N more``); arrays show
    ``array[len] of <element-shape>`` — and for an array of records the element
    shape is the *union* of the sampled records' keys, so the model sees every
    field it can filter on. Recursion is bounded by ``_JSON_SHAPE_MAX_DEPTH``."""
    if isinstance(value, dict):
        if depth >= _JSON_SHAPE_MAX_DEPTH:
            return f"object({len(value)} keys)"
        items = list(value.items())[:_JSON_SHAPE_MAX_KEYS]
        parts = [f"{k}: {_json_shape(v, depth + 1)}" for k, v in items]
        if len(value) > _JSON_SHAPE_MAX_KEYS:
            parts.append(f"… +{len(value) - _JSON_SHAPE_MAX_KEYS} more")
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list):
        n = len(value)
        if n == 0:
            return "array[0]"
        if depth >= _JSON_SHAPE_MAX_DEPTH:
            return f"array[{n}]"
        sample = value[:_JSON_ARRAY_KEY_SAMPLE]
        if all(isinstance(el, dict) for el in sample):
            # Union the sampled records' keys (first-seen type) so the model sees
            # the full field set of a uniform-ish array of objects.
            keys: dict[str, str] = {}
            for el in sample:
                for k, v in el.items():
                    if k not in keys and len(keys) < _JSON_SHAPE_MAX_KEYS:
                        keys[k] = _json_type_name(v)
            parts = [f"{k}: {t}" for k, t in keys.items()]
            return f"array[{n}] of {{{', '.join(parts)}}}"
        return f"array[{n}] of {_json_shape(value[0], depth + 1)}"
    return _json_type_name(value)


def _parse_json_bounded(text: str) -> tuple[Any, bool]:
    """Parse ``text`` as a whole JSON document, but only when it's small enough to
    do cheaply. Returns ``(value, True)`` on success, ``(None, False)`` otherwise
    (too big, blank, not object/array-rooted, or invalid) — never raises."""
    if len(text) > _JSON_PREVIEW_MAX_CHARS:
        return None, False
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None, False
    try:
        return json.loads(stripped), True
    except (ValueError, json.JSONDecodeError, RecursionError):
        return None, False


def json_preview(value: Any) -> str:
    """One-line human/agent-readable shape summary for a parsed JSON value."""
    kind = _json_type_name(value)
    return f"JSON {kind} — shape: {_json_shape(value)}"


# ── agent-chosen delivery (the `deliver` field on a runnable call) ─────────────
#
# A runnable call carries an optional `deliver` directive saying how its output
# should reach the model, chosen from the *task*, not the payload size:
#
#   auto     — the default: inline when small, else spill to tool-results/ with a
#              shape/head envelope (unchanged behaviour).
#   inline    — hand the raw output straight to the model, up to `[workspace]
#              inline_max`; above it, fall back to auto with a note.
#   digest    — cluster a text log by normalised signature and return the top-N
#              clusters (count, first/last timestamp, one sample). JSON falls back
#              to auto.
#   extract   — for a JSON *document object*, surface its largest string field as
#              text (up to inline_max) plus the other fields' shape; above the cap,
#              spill and point at workspace_json_get. Non-objects fall back to auto.
#
# Reality overrides intent: every mode that can't apply falls back to auto and
# says so in one line. The orchestration lives in bridge_autonomy; these are the
# pure pieces it composes (so they unit-test without a Bridge).

DELIVER_MODES = ("auto", "inline", "digest", "extract")

INLINE_MAX_DEFAULT = 131_072  # `[workspace] inline_max` default (chars)

_DIGEST_TOP_DEFAULT = 10  # clusters returned by a digest
_DIGEST_MAX_SCAN_BYTES = 4_000_000  # bound the bytes a digest reads


def deliver_schema_property() -> dict[str, Any]:
    """The JSON-schema property describing the ``deliver`` field — one line per
    value — shared by the search-mode ``run_runnable`` schema and the dedup/per-host
    tool schema so the option reads identically wherever it surfaces."""
    return {
        "type": "string",
        "enum": list(DELIVER_MODES),
        "description": (
            "Optional: how this runnable's output should reach you — chosen from "
            "the task, not the size. 'auto' (default): inline if small, else spilled "
            "to the workspace with a shape/head preview. 'inline': hand back the raw "
            "output whole (up to the inline cap; larger falls back to auto). "
            "'digest': cluster a text log by signature and return the top few with "
            "counts + timestamps + a sample (use for a why-did-it-fail read; JSON "
            "falls back to auto). 'extract': for a JSON document, return its largest "
            "text field plus the other fields' shape (use for a read; non-documents "
            "fall back to auto). A top-level field — never inside 'args'."
        ),
    }


# ── log-signature clustering (ported from runspec-logops-core) ─────────────────
#
# Collapse the *variable* parts of a log line (timestamps, ids, hex, quoted
# literals, numbers) into a stable "shape" so thousands of near-identical lines
# fold onto one signature; for an exception line, the signature is the exception
# class (+ a frame when present). A faithful, stdlib-only port of the pieces of
# ``runspec_logops_core.signatures`` a line-oriented digest needs — no new
# dependency, and the console must run without runspec-logops installed.

_SIG_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_SIG_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_SIG_LONGHEX = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_SIG_HEXID = re.compile(
    r"\b(?=[0-9a-fA-F]*[0-9])(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{4,}\b"
)
_SIG_QUOTED = re.compile(r"""(['"]).*?\1""")
_SIG_NUM = re.compile(r"\d+")
_SIG_WS = re.compile(r"\s+")
_SIG_LEADING_TS = re.compile(
    r"""^\s*
    (?:
        \[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\]?
      | ([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})
    )
    """,
    re.VERBOSE,
)
_SIG_EXC = re.compile(r"\b([A-Za-z_][\w.]*(?:Error|Exception|Failure|Fault))\b")
_SIG_FRAME_JAVA = re.compile(r"\bat\s+([\w.$]+)\s*\(")
_SIG_FRAME_PY = re.compile(r'File\s+"[^"]+",\s+line\s+\d+,\s+in\s+(\S+)')
_SIG_FRAME_JS = re.compile(r"\bat\s+([\w.$<>]+)\s*\(")


def _leading_ts(line: str) -> str | None:
    """The leading timestamp of ``line`` (ISO / bracketed / syslog) as its raw
    string, or ``None``. Kept as text — no datetime parse — so it's dependency-free
    and comparison-order comes from scan order, not clock parsing."""
    m = _SIG_LEADING_TS.match(line)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def _line_signature(line: str) -> str:
    """A stable signature for one log line: the exception class (+ first frame on
    the line) when present, else the normalised shape with volatile parts masked."""
    exc_m = _SIG_EXC.search(line)
    if exc_m:
        exc = exc_m.group(1).rsplit(".", 1)[-1]
        for pat in (_SIG_FRAME_PY, _SIG_FRAME_JAVA, _SIG_FRAME_JS):
            fm = pat.search(line)
            if fm:
                return f"{exc}@{fm.group(1)}"
        return exc
    s = _SIG_LEADING_TS.sub("", line, count=1)
    s = _SIG_UUID.sub("<uuid>", s)
    s = _SIG_HEX.sub("<hex>", s)
    s = _SIG_LONGHEX.sub("<hex>", s)
    s = _SIG_HEXID.sub("<hex>", s)
    s = _SIG_QUOTED.sub("<str>", s)
    s = _SIG_NUM.sub("<n>", s)
    return _SIG_WS.sub(" ", s).strip()[:200]


def line_digest(
    output: str,
    *,
    top: int = _DIGEST_TOP_DEFAULT,
    max_scan_bytes: int = _DIGEST_MAX_SCAN_BYTES,
) -> dict[str, Any]:
    """Cluster ``output``'s lines by normalised signature and return the top-N
    clusters — each with a count, first/last timestamp, and one sample line.

    Bounds the bytes scanned (``max_scan_bytes``); a signature with no exception
    or frame is the masked line shape, so near-identical log lines fold together.
    Pure/deterministic (ties on count broken by signature)."""
    clusters: dict[str, dict[str, Any]] = {}
    scanned = 0
    counted = 0
    scan_capped = False
    for line in output.split("\n"):
        scanned += len(line) + 1
        if scanned > max_scan_bytes:
            scan_capped = True
            break
        if not line.strip():
            continue
        counted += 1
        sig = _line_signature(line)
        ts = _leading_ts(line)
        g = clusters.get(sig)
        if g is None:
            clusters[sig] = {
                "signature": sig,
                "count": 1,
                "first_ts": ts,
                "last_ts": ts,
                "sample": line[:200],
            }
        else:
            g["count"] += 1
            if ts:
                if g["first_ts"] is None:
                    g["first_ts"] = ts
                g["last_ts"] = ts
    ranked = sorted(clusters.values(), key=lambda g: (-g["count"], g["signature"]))
    return {
        "delivered_as": "digest",
        "lines_scanned": counted,
        "distinct_signatures": len(clusters),
        "truncated": scan_capped or len(clusters) > top,
        "top": ranked[:top],
    }


def extract_document(value: Any, inline_max: int) -> dict[str, Any] | None:
    """For a JSON **object**, pick the field with the longest string value and
    return ``{field, text, fits, full_len, other_shape}`` — ``text`` capped at
    ``inline_max`` and ``fits`` False when the field is longer. ``None`` when
    ``value`` isn't an object or has no string field (the caller falls back to
    auto). ``other_shape`` is the shape of every *other* field."""
    if not isinstance(value, dict):
        return None
    str_fields = [(k, v) for k, v in value.items() if isinstance(v, str)]
    if not str_fields:
        return None
    field, text = max(str_fields, key=lambda kv: len(kv[1]))
    other = {k: v for k, v in value.items() if k != field}
    full_len = len(text)
    fits = full_len <= inline_max
    return {
        "field": field,
        "text": text if fits else text[:inline_max],
        "fits": fits,
        "full_len": full_len,
        "other_shape": _json_shape(other) if other else "{}",
    }


def write_spill_file(
    workspace_root: Path, tool_name: str, inv_id: str, output: str
) -> str:
    """Write ``output`` verbatim to ``tool-results/{tool}-{inv}.{json|txt}`` and
    return the workspace-relative path — the raw-file half of :func:`spill_output`,
    without the envelope. Used by the ``digest`` / ``extract`` delivery modes,
    which return a reshaped view but still persist the full output so the agent can
    page it via the ``workspace_*`` tools."""
    results_dir = workspace_root / TOOL_RESULTS_DIRNAME
    results_dir.mkdir(parents=True, exist_ok=True)
    ext = "json" if _is_json_line(output) else "txt"
    fname = f"{_sanitize_filename_part(tool_name)}-{_safe_id(inv_id) or 'run'}.{ext}"
    _atomic_write_bytes(results_dir / fname, output.encode("utf-8", "replace"))
    return f"{TOOL_RESULTS_DIRNAME}/{fname}"


# ── after-the-fact reshaping of a spilled file (workspace_json_get / materialize)


def _json_navigate(root: Any, pointer: str) -> tuple[Any, bool]:
    """Descend ``root`` by ``pointer`` — a JSON Pointer (``/a/b/0``) when it starts
    with ``/``, else a dotted path (``a.b.0``). Array indices are integers. Returns
    ``(value, found)``; ``found`` is False on a missing key / bad index."""
    p = str(pointer or "").strip()
    if not p or p in ("/", "."):
        return root, True
    if p.startswith("/"):
        parts = [seg.replace("~1", "/").replace("~0", "~") for seg in p.split("/")[1:]]
    else:
        parts = p.split(".")
    cur = root
    for seg in parts:
        if isinstance(cur, dict):
            if seg not in cur:
                return None, False
            cur = cur[seg]
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None, False
        else:
            return None, False
    return cur, True


def json_get(
    workspace_root: Path,
    rel: str,
    pointer: str = "",
    offset: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """Return one field of a JSON spill file, addressed by ``pointer`` (dotted path
    or JSON Pointer). A **string** field is paged like :func:`read_file` (0-based
    ``offset`` line window, ``limit`` lines); any other value comes back as its
    shape plus a bounded JSON rendering. Raises ``FileNotFoundError`` /
    ``ValueError`` (bad file / not JSON / missing pointer) — jailed by :func:`jail`."""
    target = jail(workspace_root, rel)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"{rel} is not a file in the workspace")
    raw = target.read_bytes()
    if _is_binary(raw):
        raise ValueError(f"{rel} looks binary — not JSON")
    parsed, ok = _parse_json_bounded(raw.decode("utf-8", "replace"))
    if not ok:
        raise ValueError(f"{rel} is not a JSON object/array (or is too large to parse)")
    value, found = _json_navigate(parsed, pointer)
    if not found:
        raise ValueError(f"pointer {pointer!r} does not resolve in {rel}")
    if isinstance(value, str):
        lines = value.split("\n")
        off = max(0, int(offset or 0))
        lim = max(1, min(int(limit or 200), _READ_MAX_LINES))
        window = lines[off : off + lim]
        return {
            "path": rel,
            "pointer": pointer,
            "type": "str",
            "total_lines": len(lines),
            "total_chars": len(value),
            "offset": off,
            "limit": lim,
            "truncated": off + len(window) < len(lines),
            "content": "\n".join(window)[:_READ_MAX_BYTES],
        }
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    return {
        "path": rel,
        "pointer": pointer,
        "type": _json_type_name(value),
        "shape": _json_shape(value),
        "truncated": len(rendered) > _READ_MAX_BYTES,
        "value": rendered[:_READ_MAX_BYTES],
    }


def materialize(workspace_root: Path, rel: str, as_: str = "digest") -> dict[str, Any]:
    """Apply a delivery transform to an already-spilled file, after the fact.
    ``as_ = "digest"`` clusters a text spill by signature (see :func:`line_digest`).
    Read-only — returns the transformed view, writes nothing. Raises
    ``FileNotFoundError`` / ``ValueError``; jailed by :func:`jail`."""
    mode = str(as_ or "digest").strip().lower()
    if mode != "digest":
        raise ValueError(f"unsupported materialize target {as_!r} (only 'digest')")
    target = jail(workspace_root, rel)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"{rel} is not a file in the workspace")
    with open(target, "rb") as fh:
        raw = fh.read(_DIGEST_MAX_SCAN_BYTES)
    if _is_binary(raw):
        raise ValueError(f"{rel} looks binary — cannot digest")
    text = raw.decode("utf-8", "replace")
    parsed, is_json = _parse_json_bounded(text)
    if is_json:
        raise ValueError(
            f"{rel} is JSON, not a line-oriented log — use workspace_json_get to page fields"
        )
    result = line_digest(text)
    result["source"] = rel
    return result


# ── path jail ─────────────────────────────────────────────────────────────────


def jail(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` and guarantee the result stays inside it.

    Rejects absolute paths, empty ``root``, and any ``..`` / symlink escape —
    a crafted path from the model can't read or write outside the workspace.
    Returns the resolved absolute path; raises ``ValueError`` otherwise.

    A backslash is treated as a path separator (normalised to ``/``) **uniformly
    for every tool**, so a Windows-minded agent's ``notes\\summary.md`` means a
    nested path in ``read``/``grep``/``ls`` too — not only in ``write_file``,
    which normalised on its own. A leading separator or Windows drive letter is
    then a clear category error and refused."""
    if not root:
        raise ValueError("workspace root is not set")
    rel_raw = str(rel or "").strip().replace("\\", "/")
    # Absolute paths are refused explicitly (rather than silently stripped to
    # a relative path): the model should ask for workspace-relative paths, and
    # a leading '/' or Windows drive letter is a clear category error.
    if rel_raw.startswith("/") or (len(rel_raw) >= 2 and rel_raw[1] == ":"):
        raise ValueError(f"absolute path {rel_raw!r} is not a workspace path")
    target = (root / rel_raw).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"path {rel_raw!r} escapes the workspace")
    return target


# ── spill (system-owned writes) ───────────────────────────────────────────────


def spill_output(
    workspace_root: Path,
    tool_name: str,
    inv_id: str,
    output: str,
    *,
    head_bytes: int = 4_096,
) -> tuple[str, str]:
    """Write ``output`` (the joined stdout of a runnable) to
    ``{workspace_root}/tool-results/{tool_name}-{inv_id}.{json|txt}`` and return
    ``(envelope, rel_path)``.

    The envelope is the short string handed to the model as the tool result:
    a header naming the file + a head preview + a hint to reach for
    ``workspace_read``/``workspace_grep``. The raw bytes on disk are uncapped —
    the model can page through them via the ``workspace_*`` file tools.

    Callers should size-check ``output`` first and only spill above the
    configured threshold; below it, the raw string is still the right thing to
    hand to the model. On any filesystem error the caller falls back to the
    pre-workspace cap-and-truncate behaviour.

    When ``output`` parses as JSON the envelope shows a compact **shape summary**
    (top-level type, keys/types, array length + record fields) in place of the raw
    byte prefix — a minified JSON dump's head slice is otherwise unreadable — and,
    for a top-level JSON **array**, a line-per-record ``.jsonl`` companion is
    written alongside the raw file so the autonomous line tools
    (``workspace_grep``/``read``/``head``/``tail``) can page or filter records
    without a confirm-gated ``run_python``. The raw file is always the verbatim
    record of what the runnable emitted; the ``.jsonl`` is a derived view."""
    results_dir = workspace_root / TOOL_RESULTS_DIRNAME
    results_dir.mkdir(parents=True, exist_ok=True)
    ext = "json" if _is_json_line(output) else "txt"
    fname = f"{_sanitize_filename_part(tool_name)}-{_safe_id(inv_id) or 'run'}.{ext}"
    dest = results_dir / fname
    data = output.encode("utf-8", "replace")
    _atomic_write_bytes(dest, data)
    total = len(data)
    rel_path = f"{TOOL_RESULTS_DIRNAME}/{fname}"

    # Enrich for JSON: a structural shape (legible even when minified) and, for a
    # top-level array, a line-per-record companion the line tools can work on.
    parsed, is_json = _parse_json_bounded(output)
    jsonl_rel: str | None = None
    if isinstance(parsed, list) and parsed:
        try:
            stem = fname.rsplit(".", 1)[0]
            jsonl_lines = "\n".join(
                json.dumps(el, ensure_ascii=False, default=str) for el in parsed
            )
            _atomic_write_bytes(
                results_dir / f"{stem}.jsonl",
                jsonl_lines.encode("utf-8", "replace"),
            )
            jsonl_rel = f"{TOOL_RESULTS_DIRNAME}/{stem}.jsonl"
        except (OSError, TypeError, ValueError):
            jsonl_rel = None  # best-effort — the raw file is still there

    if is_json:
        preview = json_preview(parsed)
    else:
        preview = data[: max(0, int(head_bytes))].decode("utf-8", "replace")

    kb = (
        f"{total / 1024:.1f} KB" if total < 1_048_576 else f"{total / 1_048_576:.1f} MB"
    )
    parts = [f"[Output spilled to workspace — {kb} written to {rel_path}]", preview]
    if jsonl_rel:
        parts.append(
            f"[A line-per-record view of the {len(parsed)} array items was written "
            f"to {jsonl_rel} — use workspace_grep / workspace_read / workspace_head "
            f"on it to filter or page records.]"
        )
    if isinstance(parsed, dict):
        # A JSON object is usually minified — one giant line — so the line tools
        # (read/grep/head/tail) can't page it. Point at the field reader instead,
        # naming the dominant string field when there is one so the model's next
        # call is one read, not a python round-trip through the sandbox.
        parts.append(json_object_hint(parsed, rel_path))
    parts.append(
        f"[… {total} bytes total; use workspace_read / workspace_grep / "
        f"workspace_head / workspace_tail on {rel_path!r} to see the raw output.]"
    )
    return "\n".join(parts), rel_path


def json_object_hint(value: dict[str, Any], rel_path: str) -> str:
    """The envelope line that tells the model how to read a spilled JSON
    **object** back: ``workspace_json_get`` by field, with the largest string
    field named as the likely target (the log body / diff / document a runnable
    wraps in a small envelope). Read-only + autonomous, so it beats a confirm-gated
    ``run_python_sandboxed`` doing ``json.load`` for the same one field."""
    str_fields = [(k, v) for k, v in value.items() if isinstance(v, str)]
    if str_fields:
        field, text = max(str_fields, key=lambda kv: len(kv[1]))
        # A key holding a '.' can't be a dotted path — spell it as a JSON Pointer
        # (RFC 6901 escaping) so the suggested call works verbatim.
        pointer = (
            "/" + field.replace("~", "~0").replace("/", "~1")
            if ("." in field or field.startswith("/"))
            else field
        )
        return (
            f"[This is a JSON object — read one field with "
            f"workspace_json_get(path={rel_path!r}, pointer={pointer!r}) (its "
            f"largest text field, {human_size(len(text))}); any other field by its "
            f"dotted path. Prefer that over run_python_sandboxed for reading it.]"
        )
    return (
        f"[This is a JSON object — read one field with "
        f"workspace_json_get(path={rel_path!r}, pointer='<dotted.path>'). Prefer "
        f"that over run_python_sandboxed for reading it.]"
    )


# ── tool handlers (pure, jailed) ──────────────────────────────────────────────


def list_dir(
    workspace_root: Path, rel: str = "", sort: str = "name"
) -> list[dict[str, Any]]:
    """List entries directly under ``rel`` in the workspace. Each row:
    ``{name, type, size, modified}`` (modified is a Unix timestamp). Dirs first,
    then files; cap at ``_LS_MAX_ENTRIES``.

    ``sort`` orders the files: ``"name"`` (default, alphabetical) or
    ``"modified"`` (most-recently-modified first) — the latter surfaces the newest
    spill without the model reading mtimes, e.g. ``ls tool-results/
    sort=modified``. Directories always sort ahead of files."""
    target = jail(workspace_root, rel)
    if not target.exists():
        # Empty workspace (no tool has spilled yet, no notes written) is a
        # legitimate state — return an empty list, not FileNotFoundError.
        if not rel or rel == ".":
            return []
        raise FileNotFoundError(f"{rel or '.'} does not exist in the workspace")
    if not target.is_dir():
        raise NotADirectoryError(f"{rel} is a file, not a directory")

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    if sort == "modified":
        # Dirs first (is_file False < True), then newest mtime first.
        key = lambda p: (p.is_file(), -_mtime(p))  # noqa: E731
    else:
        key = lambda p: (p.is_file(), p.name.lower())  # noqa: E731

    rows: list[dict[str, Any]] = []
    for entry in sorted(target.iterdir(), key=key):
        if entry.is_dir() and entry.name in _SKIP_DIRS:
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        rows.append(
            {
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "size": st.st_size if entry.is_file() else 0,
                "modified": int(st.st_mtime),
            }
        )
        if len(rows) >= _LS_MAX_ENTRIES:
            break
    return rows


def read_file(
    workspace_root: Path,
    rel: str,
    offset: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """Read a text file from the workspace, one page at a time.

    ``offset`` is a 0-based **line** index (0 = the first line); ``limit`` caps
    how many lines come back (default 200, hard max ``_READ_MAX_LINES``). Unlike a
    plain first-chunk read, this **streams the whole file** so a window deep in a
    multi-MB spill is reachable — the file is scanned line by line up to
    ``_READ_SCAN_MAX_BYTES`` (so a pathological giant file still can't wedge a
    call). Returns ``{path, offset, limit, total_lines, total_lines_exact,
    total_bytes, truncated, content}``; ``total_lines_exact`` is ``False`` when
    the scan cap was hit before EOF (then ``total_lines`` is a lower bound). The
    returned ``content`` is itself capped at ``_READ_MAX_BYTES``.

    A binary file raises ``ValueError``; a symlink escaping the workspace is
    rejected by :func:`jail`."""
    target = jail(workspace_root, rel)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"{rel} is not a file in the workspace")
    total_bytes = target.stat().st_size
    with open(target, "rb") as fh:
        if _is_binary(fh.read(4096)):
            raise ValueError(f"{rel} looks binary — not returning its bytes")
    off = max(0, int(offset or 0))
    lim = max(1, min(int(limit or 200), _READ_MAX_LINES))

    sel: list[str] = []
    total_lines = 0
    scanned = 0
    content_bytes = 0
    content_capped = False
    scan_capped = False
    # ``newline=""`` keeps universal-newline splitting off (split on \n only) so a
    # stray \r inside a value doesn't invent a line; the trailing CR/LF is stripped
    # per line, which also normalises CRLF files.
    with open(target, "r", encoding="utf-8", errors="replace", newline="") as fh:
        for raw in fh:
            scanned += len(raw)
            idx = total_lines
            total_lines += 1
            if off <= idx < off + lim and not content_capped:
                line = raw.rstrip("\r\n")
                enc = len(line) + 1
                if sel and content_bytes + enc > _READ_MAX_BYTES:
                    content_capped = True
                else:
                    sel.append(line)
                    content_bytes += enc
            if scanned >= _READ_SCAN_MAX_BYTES:
                scan_capped = True
                break
    window_end = off + len(sel)
    truncated = content_capped or scan_capped or window_end < total_lines
    return {
        "path": rel,
        "offset": off,
        "limit": lim,
        "total_lines": total_lines,
        "total_lines_exact": not scan_capped,
        "total_bytes": total_bytes,
        "truncated": truncated,
        "content": "\n".join(sel),
    }


def grep(
    workspace_root: Path,
    pattern: str,
    rel: str = "",
    max_results: int = 100,
    context: int = 0,
    *,
    ignore_case: bool = False,
    fixed: bool = False,
    glob: str = "",
) -> list[dict[str, Any]]:
    """Search files under ``rel`` (default: whole workspace) for ``pattern``.
    Skips ``.git``/noise dirs, binary files, and files over
    ``_GREP_FILE_MAX_BYTES``. Returns up to ``max_results`` (≤
    ``_GREP_MAX_RESULTS``) rows: ``{path, line, text}``.

    ``pattern`` is a **regular expression** by default; pass ``fixed=True`` to
    match it as a **literal** string (regex metacharacters escaped — also immune
    to ``re.error``). ``ignore_case=True`` folds case. ``glob`` limits the scan to
    files whose workspace-relative posix path (or bare name) matches a shell-style
    pattern — a comma-separated list is allowed (e.g. ``"tool-results/*.json"`` or
    ``"*.log,*.txt"``). Raises ``re.error`` on a bad regex (only when ``fixed`` is
    false).

    ``context`` is honoured symmetrically (before + after) up to 5 lines each
    side — enough for a stack-trace neighbourhood without ballooning results."""
    from fnmatch import fnmatch

    flags = re.IGNORECASE if ignore_case else 0
    rx = re.compile(re.escape(pattern) if fixed else pattern, flags)
    ctx = max(0, min(int(context or 0), 5))
    base = jail(workspace_root, rel)
    limit = min(max(1, int(max_results or 100)), _GREP_MAX_RESULTS)
    globs = [g.strip() for g in glob.split(",") if g.strip()]
    out: list[dict[str, Any]] = []
    if not base.exists():
        return out
    if base.is_file():
        # An explicitly-named file is searched regardless of ``glob`` (the model
        # already narrowed to it).
        _grep_one(base, workspace_root, rx, ctx, out, limit)
        return out
    root_resolved = workspace_root.resolve()
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in sorted(files):
            fpath = Path(root) / fname
            if globs:
                try:
                    relpath = fpath.resolve().relative_to(root_resolved).as_posix()
                except ValueError:
                    continue  # symlink outside the workspace — never surface it
                if not any(fnmatch(relpath, g) or fnmatch(fname, g) for g in globs):
                    continue
            try:
                if fpath.stat().st_size > _GREP_FILE_MAX_BYTES:
                    continue
            except OSError:
                continue
            _grep_one(fpath, workspace_root, rx, ctx, out, limit)
            if len(out) >= limit:
                return out
    return out


def _grep_one(
    fpath: Path,
    workspace_root: Path,
    rx: re.Pattern[str],
    ctx: int,
    out: list[dict[str, Any]],
    limit: int,
) -> None:
    try:
        data = fpath.read_bytes()
    except OSError:
        return
    if _is_binary(data):
        return
    try:
        relpath = fpath.resolve().relative_to(workspace_root.resolve()).as_posix()
    except ValueError:
        return  # symlink outside the workspace — never surface it
    lines = _split_lines(data.decode("utf-8", "replace"))
    for i, line in enumerate(lines):
        if rx.search(line):
            row: dict[str, Any] = {
                "path": relpath,
                "line": i + 1,
                "text": line[:400],
            }
            if ctx:
                before_start = max(0, i - ctx)
                after_end = min(len(lines), i + ctx + 1)
                row["before"] = [line_i[:400] for line_i in lines[before_start:i]]
                row["after"] = [line_i[:400] for line_i in lines[i + 1 : after_end]]
            out.append(row)
            if len(out) >= limit:
                return


def head(workspace_root: Path, rel: str, lines: int = 50) -> dict[str, Any]:
    """First ``lines`` lines of a text file. Handy short-circuit for the
    common "just show me the top" case."""
    n = max(1, min(int(lines or 50), _HEAD_TAIL_MAX_LINES))
    result = read_file(workspace_root, rel, offset=0, limit=n)
    return result


def tail(workspace_root: Path, rel: str, lines: int = 50) -> dict[str, Any]:
    """Last ``lines`` lines of a text file. Reads at most ``_READ_MAX_BYTES``
    off disk (same cap as ``workspace_read``), so a truly huge file's tail
    is bounded by what fits in one read from the file's start — the
    ``truncated`` flag says so."""
    target = jail(workspace_root, rel)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"{rel} is not a file in the workspace")
    total_bytes = target.stat().st_size
    with open(target, "rb") as fh:
        # Read the tail directly for reasonable-size files; fall back to
        # a bounded head read (with the truncated flag set) for huge files
        # so we don't have to seek and count lines in a 10 GB dump.
        if total_bytes <= _READ_MAX_BYTES:
            data = fh.read()
            truncated_bytes = False
        else:
            fh.seek(total_bytes - _READ_MAX_BYTES)
            data = fh.read()
            truncated_bytes = True
    if _is_binary(data):
        raise ValueError(f"{rel} looks binary — not returning its bytes")
    text = data.decode("utf-8", "replace")
    all_lines = _split_lines(text)
    # When we seeked mid-file the first line is almost certainly a partial —
    # drop it so the tail starts at a real line boundary. But only when there's
    # more than one line: a huge *single-line* file (e.g. minified JSON) has no
    # newline in the tail window, so dropping the lone line would return nothing.
    if truncated_bytes and len(all_lines) > 1:
        all_lines = all_lines[1:]
    n = max(1, min(int(lines or 50), _HEAD_TAIL_MAX_LINES))
    total_lines = len(all_lines)
    sel = all_lines[-n:]
    return {
        "path": rel,
        "lines": len(sel),
        "total_lines_in_window": total_lines,
        "total_bytes": total_bytes,
        "truncated": truncated_bytes,
        "content": "\n".join(sel),
    }


def write_file(workspace_root: Path, rel: str, content: str) -> dict[str, Any]:
    """Create or overwrite a text file in the workspace, but **refuse**
    ``tool-results/`` (system-owned — the model shouldn't be able to overwrite
    the record of what a runnable actually emitted). Parent dirs created."""
    if not rel or not rel.strip():
        raise ValueError("workspace_write needs a path (e.g. notes/summary.md)")
    rel_clean = str(rel).strip().replace("\\", "/")
    # Path-jail first — rejects absolute paths / escape / drive letters — then
    # check the reserved dir against the *resolved* target's first segment (not
    # the raw string). Resolving collapses traversal, so `notes/../tool-results/x`
    # is caught, and the compare is case-insensitive so `Tool-Results/x` can't
    # slip past on a case-insensitive (Windows/macOS) filesystem.
    target = jail(workspace_root, rel_clean)
    try:
        rel_parts = target.relative_to(workspace_root.resolve()).parts
    except ValueError:
        rel_parts = ()
    if rel_parts and rel_parts[0].lower() == TOOL_RESULTS_DIRNAME:
        raise PermissionError(
            f"{TOOL_RESULTS_DIRNAME}/ is reserved for system-spilled outputs — "
            f"write agent-authored files under a different path (e.g. {NOTES_DIRNAME}/)"
        )
    if target.exists() and target.is_dir():
        raise IsADirectoryError(f"{rel} is a directory")
    created = not target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    target.write_bytes(data)
    return {"path": rel_clean, "bytes": len(data), "created": created}


# ── retention / orphan sweep ──────────────────────────────────────────────────


def sweep_orphans(
    workspaces_root: Path,
    alive_chat_ids: set[str],
    *,
    headless_retention_days: int,
    now_ts: float | None = None,
) -> int:
    """Drop workspaces that no longer belong to a live session.

    * A workspace whose id starts with ``trigger-`` / ``schedule-`` is a
      headless workspace and is dropped when its mtime is older than
      ``headless_retention_days``.
    * Any other workspace id is treated as a chat conversation id: dropped if
      it is not in ``alive_chat_ids``.

    Returns the number of workspaces removed. Robust to a missing root and
    per-entry filesystem errors (no exception escapes)."""
    if not workspaces_root.exists():
        return 0
    now = time.time() if now_ts is None else float(now_ts)
    cutoff = now - max(0, int(headless_retention_days)) * 86400
    removed = 0
    for entry in workspaces_root.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        try:
            if name.startswith(("trigger-", "schedule-")):
                if headless_retention_days <= 0:
                    continue
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            else:
                if name not in alive_chat_ids:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
        except OSError:
            continue
    return removed


def delete_workspace(workspace_root: Path) -> None:
    """Drop one workspace directory (best-effort). Used by
    ``delete_conversation`` + trigger/schedule turn finalisation."""
    shutil.rmtree(workspace_root, ignore_errors=True)

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
_READ_MAX_BYTES = 512_000  # a single workspace_read is capped at ~512 KB
_READ_MAX_LINES = 2_000  # …and 2000 lines
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


# ── path jail ─────────────────────────────────────────────────────────────────


def jail(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` and guarantee the result stays inside it.

    Rejects absolute paths, empty ``root``, and any ``..`` / symlink escape —
    a crafted path from the model can't read or write outside the workspace.
    Returns the resolved absolute path; raises ``ValueError`` otherwise."""
    if not root:
        raise ValueError("workspace root is not set")
    rel_raw = str(rel or "").strip()
    # Absolute paths are refused explicitly (rather than silently stripped to
    # a relative path): the model should ask for workspace-relative paths, and
    # a leading '/' or Windows drive letter is a clear category error.
    if rel_raw.startswith(("/", "\\")) or (len(rel_raw) >= 2 and rel_raw[1] == ":"):
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
    pre-workspace cap-and-truncate behaviour."""
    results_dir = workspace_root / TOOL_RESULTS_DIRNAME
    results_dir.mkdir(parents=True, exist_ok=True)
    ext = "json" if _is_json_line(output) else "txt"
    fname = f"{_sanitize_filename_part(tool_name)}-{_safe_id(inv_id) or 'run'}.{ext}"
    dest = results_dir / fname
    data = output.encode("utf-8", "replace")
    dest.write_bytes(data)
    total = len(data)
    head_slice = data[: max(0, int(head_bytes))].decode("utf-8", "replace")
    rel_path = f"{TOOL_RESULTS_DIRNAME}/{fname}"
    kb = (
        f"{total / 1024:.1f} KB" if total < 1_048_576 else f"{total / 1_048_576:.1f} MB"
    )
    envelope = (
        f"[Output spilled to workspace — {kb} written to {rel_path}]\n"
        f"{head_slice}\n"
        f"[… {total} bytes total; use workspace_read / workspace_grep / "
        f"workspace_head / workspace_tail on {rel_path!r} to see the rest.]"
    )
    return envelope, rel_path


# ── tool handlers (pure, jailed) ──────────────────────────────────────────────


def list_dir(workspace_root: Path, rel: str = "") -> list[dict[str, Any]]:
    """List entries directly under ``rel`` in the workspace. Each row:
    ``{name, type, size, modified}`` (modified is a Unix timestamp). Dirs
    first, then files; cap at ``_LS_MAX_ENTRIES``."""
    target = jail(workspace_root, rel)
    if not target.exists():
        # Empty workspace (no tool has spilled yet, no notes written) is a
        # legitimate state — return an empty list, not FileNotFoundError.
        if not rel or rel == ".":
            return []
        raise FileNotFoundError(f"{rel or '.'} does not exist in the workspace")
    if not target.is_dir():
        raise NotADirectoryError(f"{rel} is a file, not a directory")
    rows: list[dict[str, Any]] = []
    for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
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

    ``offset`` is 0-based (0 = start at the first line); ``limit`` caps how
    many lines come back (default 200, hard max ``_READ_MAX_LINES``). Reads
    at most ``_READ_MAX_BYTES`` off disk in one call. Returns
    ``{path, offset, limit, total_lines, total_bytes, truncated, content}``.

    A binary file raises ``ValueError``; a symlink escaping the workspace is
    rejected by :func:`jail`."""
    target = jail(workspace_root, rel)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"{rel} is not a file in the workspace")
    total_bytes = target.stat().st_size
    with open(target, "rb") as fh:
        data = fh.read(_READ_MAX_BYTES)
    truncated_bytes = total_bytes > _READ_MAX_BYTES
    if _is_binary(data):
        raise ValueError(f"{rel} looks binary — not returning its bytes")
    text = data.decode("utf-8", "replace")
    all_lines = text.splitlines()
    total_lines = len(all_lines)
    off = max(0, int(offset or 0))
    lim = max(1, min(int(limit or 200), _READ_MAX_LINES))
    slice_end = min(off + lim, total_lines)
    sel = all_lines[off:slice_end]
    truncated = truncated_bytes or slice_end < total_lines
    return {
        "path": rel,
        "offset": off,
        "limit": lim,
        "total_lines": total_lines,
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
) -> list[dict[str, Any]]:
    """Regex-search files under ``rel`` (default: whole workspace) for
    ``pattern``. Skips ``.git``/noise dirs, binary files, and files over
    ``_GREP_FILE_MAX_BYTES``. Returns up to ``max_results`` (≤
    ``_GREP_MAX_RESULTS``) rows: ``{path, line, text}``. Raises ``re.error``
    on a bad pattern.

    ``context`` is honoured symmetrically (before + after) up to 5 lines each
    side — enough for a stack-trace neighbourhood without ballooning results."""
    rx = re.compile(pattern)
    ctx = max(0, min(int(context or 0), 5))
    base = jail(workspace_root, rel)
    limit = min(max(1, int(max_results or 100)), _GREP_MAX_RESULTS)
    out: list[dict[str, Any]] = []
    if not base.exists():
        return out
    if base.is_file():
        _grep_one(base, workspace_root, rx, ctx, out, limit)
        return out
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in sorted(files):
            fpath = Path(root) / fname
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
    lines = data.decode("utf-8", "replace").splitlines()
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
    all_lines = text.splitlines()
    # When we seeked mid-file the first line is almost certainly a partial —
    # drop it so the tail we hand back starts at a real line boundary.
    if truncated_bytes and all_lines:
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
    # Path-jail first — rejects absolute paths / escape / drive letters —
    # THEN check the reserved dir, so a crafted "/tool-results/x" doesn't
    # sneak past the check (the leading '/' would be stripped before the
    # first-segment inspection).
    target = jail(workspace_root, rel_clean)
    first_segment = rel_clean.split("/", 1)[0]
    if first_segment == TOOL_RESULTS_DIRNAME:
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

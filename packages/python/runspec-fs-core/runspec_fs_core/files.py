"""File read/write/edit helpers — the pure logic behind the ``fs`` runnable's
file verbs (``read-text-file``, ``read-media-file``, ``read-multiple-files``,
``write-file``, ``edit-file``).

Each function takes plain params (``str``/``os.PathLike``), returns plain data,
and raises on failure. Filesystem errors propagate as the stdlib ``OSError``
family; ``edit_file`` raises :class:`EditNotAppliedError` when an old-text block
is not present.
"""

from __future__ import annotations

import base64
import difflib
import mimetypes
import os
from pathlib import Path
from typing import Any

from runspec_fs_core.errors import EditNotAppliedError


def _as_path(path: str | os.PathLike[str]) -> Path:
    """Normalise an incoming path. ``expanduser`` only — the ``path``-typed args
    are already resolved by runspec's coercer, and str-typed paths shouldn't have
    symlinks silently resolved out from under the caller."""
    return Path(path).expanduser()


def read_text_file(
    path: str | os.PathLike[str],
    *,
    head: int | None = None,
    tail: int | None = None,
) -> dict[str, Any]:
    """Read a file as UTF-8 text.

    ``head``/``tail`` return only the first / last N lines respectively (they are
    mutually exclusive, mirroring the MCP tool). Returns
    ``{path, content, truncated}``.
    """
    if head is not None and tail is not None:
        raise ValueError("Pass only one of head / tail, not both")
    p = _as_path(path)
    text = p.read_text(encoding="utf-8")

    if head is None and tail is None:
        return {"path": str(p), "content": text, "truncated": False}

    lines = text.splitlines(keepends=True)
    if head is not None:
        if head < 0:
            raise ValueError("head must be >= 0")
        selected = lines[:head]
    else:  # tail is not None
        if tail < 0:  # type: ignore[operator]
            raise ValueError("tail must be >= 0")
        selected = lines[-tail:] if tail else []
    return {
        "path": str(p),
        "content": "".join(selected),
        "truncated": len(selected) < len(lines),
    }


def read_media_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a file's raw bytes and return them base64-encoded with a guessed MIME
    type. Returns ``{path, mime_type, base64, bytes}``."""
    p = _as_path(path)
    data = p.read_bytes()
    mime, _ = mimetypes.guess_type(str(p))
    return {
        "path": str(p),
        "mime_type": mime or "application/octet-stream",
        "base64": base64.b64encode(data).decode("ascii"),
        "bytes": len(data),
    }


def read_multiple_files(paths: list[str | os.PathLike[str]]) -> dict[str, Any]:
    """Read several files. A failed read is captured per-file rather than aborting
    the whole batch. Returns ``{files: [{path, content} | {path, error}]}``."""
    results: list[dict[str, Any]] = []
    for path in paths:
        p = _as_path(path)
        try:
            results.append({"path": str(p), "content": p.read_text(encoding="utf-8")})
        except OSError as exc:
            results.append({"path": str(p), "error": str(exc)})
        except UnicodeDecodeError:
            results.append({"path": str(p), "error": "not a UTF-8 text file"})
    return {"files": results, "count": len(results)}


def write_file(path: str | os.PathLike[str], content: str) -> dict[str, Any]:
    """Create or overwrite a file with UTF-8 text. Returns
    ``{path, bytes_written}``."""
    p = _as_path(path)
    data = content.encode("utf-8")
    p.write_bytes(data)
    return {"path": str(p), "bytes_written": len(data)}


def edit_file(
    path: str | os.PathLike[str],
    edits: list[tuple[str, str]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a sequence of ``(old_text, new_text)`` substring replacements to a
    file, in order (first occurrence of each ``old_text``).

    Raises :class:`EditNotAppliedError` if any ``old_text`` is not present at the
    time it is applied — nothing is written in that case. With ``dry_run`` the new
    content is computed and diffed but not written. Returns
    ``{path, edits_applied, dry_run, diff}`` where ``diff`` is a unified diff.
    """
    p = _as_path(path)
    original = p.read_text(encoding="utf-8")

    updated = original
    for index, (old_text, new_text) in enumerate(edits, start=1):
        if old_text not in updated:
            raise EditNotAppliedError(f"edit #{index}: old text not found in {p}: {old_text!r}")
        updated = updated.replace(old_text, new_text, 1)

    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{p.name}",
            tofile=f"b/{p.name}",
        )
    )

    if not dry_run:
        p.write_text(updated, encoding="utf-8")

    return {
        "path": str(p),
        "edits_applied": len(edits),
        "dry_run": dry_run,
        "diff": diff,
    }

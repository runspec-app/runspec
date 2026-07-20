"""fs_transfer.py — plan local↔remote file transfers for the Transfer dialog.

v1 is **local↔remote only**: exactly one side is the local host, the other a
remote host reached over SFTP. Planning — push vs pull and which file lands
where — is pure and unit-tested here; the SFTP ``get``/``put`` I/O (with progress
events) lives in the bridge.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

LOCAL = "local"

# Placeholder a filter pipeline uses to mark where the operator's runtime value
# (e.g. an identifier to grep for) is substituted. Written literally in the
# pipeline; replaced with a shell-quoted value at transfer time.
FILTER_ARG_TOKEN = "{{arg}}"


@dataclass
class TransferItem:
    src: str  # absolute source path (on the source side)
    dst: str  # absolute destination path (dir + source basename)
    name: str  # basename, for display


@dataclass
class TransferPlan:
    direction: str  # "push" (local→remote) or "pull" (remote→local)
    remote_host: str  # the non-local host
    items: list[TransferItem]


def _basename(path: str) -> str:
    """Last path segment, tolerant of both ``/`` and ``\\`` separators."""
    return re.split(r"[\\/]", path.rstrip("/\\"))[-1]


def _join(directory: str, name: str) -> str:
    """Join ``name`` onto ``directory`` using the separator the dir already uses."""
    sep = "\\" if ("\\" in directory and "/" not in directory) else "/"
    return directory.rstrip(sep) + sep + name


def _clean_dest_name(dest_name: str | None) -> str | None:
    """Validate an optional rename-on-target basename. Returns the stripped name
    (``None`` when unset). Raises :class:`ValueError` if it isn't a bare filename
    — a path separator or ``.``/``..`` would let the file escape ``dest_dir``.
    """
    name = (dest_name or "").strip()
    if not name:
        return None
    if re.search(r"[\\/]", name) or name in (".", ".."):
        raise ValueError("a new filename must be a bare filename, not a path")
    return name


def _dest_items(
    paths: list[str], dest_dir: str, dest_name: str | None
) -> list[TransferItem]:
    """Build the per-file items, applying an optional rename. When ``dest_name``
    is set the (single) file lands under that basename instead of the source's;
    renaming several files at once is rejected."""
    name = _clean_dest_name(dest_name)
    if name and len(paths) > 1:
        raise ValueError("a new filename can only be set for a single file")
    return [
        TransferItem(
            src=p,
            dst=_join(dest_dir, name or _basename(p)),
            name=name or _basename(p),
        )
        for p in paths
    ]


def plan_transfer(
    source_host: str,
    source_paths: list[str],
    dest_host: str,
    dest_dir: str,
    dest_name: str | None = None,
) -> TransferPlan:
    """Resolve a local↔remote transfer into per-file ``(src, dst)`` items.

    Exactly one of ``source_host`` / ``dest_host`` must be ``"local"``. An optional
    ``dest_name`` renames the (single) transferred file on the destination side —
    it must be a bare filename. Raises :class:`ValueError` on an empty selection, a
    missing destination, a same-side request (local↔local or remote↔remote), or a
    rename of more than one file.
    """
    paths = [p for p in (source_paths or []) if p and p.strip()]
    if not paths:
        raise ValueError("no source files selected")
    if not dest_dir or not dest_dir.strip():
        raise ValueError("no destination folder selected")

    src_local = source_host == LOCAL
    dst_local = dest_host == LOCAL
    if src_local == dst_local:
        raise ValueError("a transfer must have one local side and one remote side")

    direction = "push" if src_local else "pull"
    remote_host = dest_host if src_local else source_host
    items = _dest_items(paths, dest_dir, dest_name)
    return TransferPlan(direction=direction, remote_host=remote_host, items=items)


def relay_items(
    source_paths: list[str], dest_dir: str, dest_name: str | None = None
) -> list[TransferItem]:
    """Per-file ``(src, dst)`` items for a remote→remote relay (host → host).

    The console streams each ``src`` (read from the source host) to ``dst``
    (written on the destination host, ``dest_dir`` + the source basename, or
    ``dest_name`` when a single file is being renamed). Raises :class:`ValueError`
    on an empty selection, a missing destination, or a multi-file rename.
    """
    paths = [p for p in (source_paths or []) if p and p.strip()]
    if not paths:
        raise ValueError("no source files selected")
    if not dest_dir or not dest_dir.strip():
        raise ValueError("no destination folder selected")
    return _dest_items(paths, dest_dir, dest_name)


def render_pipeline(pipeline: str, arg: str | None) -> str:
    """Substitute the operator-supplied ``arg`` into a filter ``pipeline``.

    Every ``{{arg}}`` occurrence is replaced with ``shlex.quote(arg)`` so a
    user-typed value (an identifier, a path fragment) is a single, inert shell
    token and can never break out of the pipeline — only the pipeline *structure*
    written by the filter author is trusted, interpreted shell.

    Raises :class:`ValueError` if the pipeline expects an arg but none was given,
    or an arg was given for a pipeline with no ``{{arg}}`` placeholder (so a
    mismatched call surfaces loudly rather than silently dropping the value).
    """
    pl = (pipeline or "").strip()
    if not pl:
        raise ValueError("empty filter pipeline")
    has_token = FILTER_ARG_TOKEN in pl
    arg = (arg or "").strip() if arg is not None else ""
    if has_token and not arg:
        raise ValueError("this filter needs a value")
    if arg and not has_token:
        raise ValueError("this filter takes no value")
    if has_token:
        pl = pl.replace(FILTER_ARG_TOKEN, shlex.quote(arg))
    return pl


def build_filter_command(rendered_pipeline: str, src_path: str) -> str:
    """Build the remote shell command that filters one source file.

    Returns e.g. ``sh -c 'grep -F -- foo' < '/var/log/app.log'``. The source path
    is shlex-quoted into an stdin redirect applied by the *outer* shell and is
    never interpreted by the pipeline; the whole (already-rendered) pipeline is a
    single quoted argument to the inner ``sh -c`` so its pipes/redirects belong to
    that inner shell. Raises :class:`ValueError` on an empty pipeline.
    """
    pl = (rendered_pipeline or "").strip()
    if not pl:
        raise ValueError("empty filter pipeline")
    return f"sh -c {shlex.quote(pl)} < {shlex.quote(src_path)}"

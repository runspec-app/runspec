"""fs_transfer.py — plan local↔remote file transfers for the Transfer dialog.

v1 is **local↔remote only**: exactly one side is the local host, the other a
remote host reached over SFTP. Planning — push vs pull and which file lands
where — is pure and unit-tested here; the SFTP ``get``/``put`` I/O (with progress
events) lives in the bridge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LOCAL = "local"


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


def plan_transfer(
    source_host: str,
    source_paths: list[str],
    dest_host: str,
    dest_dir: str,
) -> TransferPlan:
    """Resolve a local↔remote transfer into per-file ``(src, dst)`` items.

    Exactly one of ``source_host`` / ``dest_host`` must be ``"local"``. Raises
    :class:`ValueError` on an empty selection, a missing destination, or a
    same-side request (local↔local or remote↔remote — out of scope for v1).
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
    items = [
        TransferItem(src=p, dst=_join(dest_dir, _basename(p)), name=_basename(p))
        for p in paths
    ]
    return TransferPlan(direction=direction, remote_host=remote_host, items=items)


def relay_items(source_paths: list[str], dest_dir: str) -> list[TransferItem]:
    """Per-file ``(src, dst)`` items for a remote→remote relay (host → host).

    The console streams each ``src`` (read from the source host) to ``dst``
    (written on the destination host, ``dest_dir`` + the source basename). Raises
    :class:`ValueError` on an empty selection or missing destination.
    """
    paths = [p for p in (source_paths or []) if p and p.strip()]
    if not paths:
        raise ValueError("no source files selected")
    if not dest_dir or not dest_dir.strip():
        raise ValueError("no destination folder selected")
    return [
        TransferItem(src=p, dst=_join(dest_dir, _basename(p)), name=_basename(p))
        for p in paths
    ]

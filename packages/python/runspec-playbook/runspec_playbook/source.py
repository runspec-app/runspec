"""source.py — read a playbook repo the runspec-console sends with a run.

The console runs ``run-playbook --repo <name> --playbook <path>`` where
``<name>`` is a git repo registered in its Repos tab. It packs that repo's
checkout (no ``.git``) and sends it with the run:

* the env var ``RUNSPEC_PLAYBOOK_SOURCE = "stdin:<length>:<sha256>"`` rides the
  console's env frame, and
* ``<length>`` bytes of payload follow the frame on this process's stdin.

The env prelude reads stdin one byte at a time and stops at the frame's end, so
the payload is still unread when this runnable starts. Stdin passes through
sudo / su like the frame does, so nothing is copied to the host first, no file
permissions get in the way of ``run_as``, and nothing is left behind.

Payload (version 1)::

    payload = base64(gzip(json))            # ASCII
    json    = {"v": 1, "repo": "homelab", "commit": "<sha>", "dirty": false,
               "host": "web-01", "groups": ["web", "home"],
               "files": {"site.yml": "<base64 bytes>", "roles/x/tasks/main.yml": ...}}

``host`` / ``groups`` are the console's name for this host and its console
group + profiles; the runnable builds its one-host inventory from them so
``hosts: web``, ``group_vars/`` and ``host_vars/`` in the repo work.

``<sha256>`` in the env var is :func:`content_hash` of ``files`` — canonical
JSON (sorted keys, no spaces) of the path → base64 map. The console computes it
the same way (``runspec_console.playbook_repo.content_hash``); a shared test
vector keeps the two in step. Pure stdlib, no Ansible import.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

ENV_VAR = "RUNSPEC_PLAYBOOK_SOURCE"
# Refuse anything bigger than this before reading it (the console caps lower).
MAX_PAYLOAD = 64 * 1024 * 1024


class SourceError(ValueError):
    """The sent repo is malformed, altered, or doesn't hold the playbook."""


@dataclass(frozen=True)
class SentRepo:
    repo: str
    commit: str
    dirty: bool
    files: dict[str, bytes]
    sha256: str
    host: str = ""
    groups: tuple[str, ...] = ()


def content_hash(files_b64: dict[str, str]) -> str:
    """SHA-256 of the sent files: canonical JSON of the path → base64 map."""
    canon = json.dumps(files_b64, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("ascii")).hexdigest()


def safe_relpath(rel: str) -> str:
    """A path inside the repo folder, or SourceError.

    Relative, forward slashes, no ``.``/``..`` parts, no drive or leading
    slash — so a sent file can never land outside its temporary folder.
    """
    rel = str(rel).replace("\\", "/")
    p = PurePosixPath(rel)
    if not rel or rel.startswith("/") or p.is_absolute() or ":" in p.parts[0]:
        raise SourceError(f"path must be relative to the repo: {rel!r}")
    if any(part in ("", ".", "..") for part in rel.split("/")):
        raise SourceError(f"path may not contain '.' or '..': {rel!r}")
    return str(p)


def parse_header(value: str) -> tuple[int, str]:
    """``stdin:<length>:<sha256>`` → (length, sha256)."""
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != "stdin":
        raise SourceError(f"unrecognised {ENV_VAR} value (expected stdin:<length>:<sha256>)")
    try:
        length = int(parts[1])
    except ValueError as exc:
        raise SourceError(f"bad length in {ENV_VAR}") from exc
    if length <= 0 or length > MAX_PAYLOAD:
        raise SourceError(f"sent repo size {length} is out of range")
    return length, parts[2]


def read_payload(stream: BinaryIO, length: int) -> bytes:
    """Exactly ``length`` bytes from ``stream`` (SourceError if it ends early)."""
    chunks: list[bytes] = []
    left = length
    while left:
        chunk = stream.read(min(left, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        left -= len(chunk)
    if left:
        raise SourceError(f"sent repo was cut short ({length - left} of {length} bytes)")
    return b"".join(chunks)


def decode(payload: bytes, sha256: str) -> SentRepo:
    """Decode the payload and check it against the header's ``sha256``."""
    try:
        raw = gzip.decompress(base64.b64decode(payload, validate=True))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, OSError, binascii.Error) as exc:
        raise SourceError(f"could not read the sent repo: {exc}") from exc
    if not isinstance(data, dict) or data.get("v") != 1:
        raise SourceError("unsupported sent-repo version")
    files_b64 = data.get("files")
    if not isinstance(files_b64, dict) or not files_b64:
        raise SourceError("the sent repo has no files")
    if content_hash({str(k): str(v) for k, v in files_b64.items()}) != sha256:
        raise SourceError("the sent repo failed its content check (sha256 mismatch)")
    files: dict[str, bytes] = {}
    for rel, b64 in files_b64.items():
        try:
            files[safe_relpath(rel)] = base64.b64decode(str(b64), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SourceError(f"bad file data for {rel!r}") from exc
    return SentRepo(
        repo=str(data.get("repo") or ""),
        commit=str(data.get("commit") or ""),
        dirty=bool(data.get("dirty")),
        files=files,
        sha256=sha256,
        host=_inventory_name(data.get("host")),
        groups=tuple(g for g in (_inventory_name(x) for x in (data.get("groups") or [])) if g),
    )


def _inventory_name(value: object) -> str:
    """A host/group name safe for an inventory, or "" (dropped)."""
    text = str(value or "").strip()
    if not text or len(text) > 128:
        return ""
    if not all(c.isalnum() or c in "-_." for c in text):
        return ""
    return text


def receive(header: str, stream: BinaryIO) -> SentRepo:
    """Read and verify the repo announced by ``header`` from ``stream``."""
    length, sha = parse_header(header)
    return decode(read_payload(stream, length), sha)


def write_files(sent: SentRepo, project: Path) -> None:
    """Write the sent files under ``project`` (which must exist)."""
    root = project.resolve()
    for rel, data in sent.files.items():
        dest = (root / rel).resolve()
        if root not in dest.parents:
            raise SourceError(f"file escapes the repo folder: {rel!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

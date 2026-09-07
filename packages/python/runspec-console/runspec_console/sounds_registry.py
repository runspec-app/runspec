"""
sounds_registry.py — read/validate/write ``runspec_sounds.toml``, the named-sound
registry.

A **named sound** is an operator-added audio file given a stable ``id`` + a
human ``label``, so a sound can be referenced *by name* everywhere the console
plays cues: the per-category picks in Settings, a trigger's / schedule's action
``sound``, and the agent's ``play_sound`` tool — all take a ``sound:<id>`` token
(or the bare id) instead of a raw filesystem path.

A registry entry::

    [[sound]]
    id    = "deploy-done"   # unique identity (Config Sync merges by id)
    label = "Deploy done"   # shown in the UI + named by the agent
    file  = "C:/Users/…/AppData/…/runspec-console/sounds/deploy-done.mp3"

The file **rides Config Sync** (in ``gitsync.KNOWN_FILES``), merged by ``id`` like
runbooks — but the audio ``file`` it points at is binary and **machine-local**
(the console copies an imported file under ``config.sounds_store_dir()``, never
synced, exactly like a sound *pack* folder). So a synced id resolves to a cue
only where that file is present; on any other machine it degrades gracefully to
silence (a missing file is already a silent no-op in :mod:`sounds`). This mirrors
the deliberate split the console uses for sound packs — the *choice* syncs, the
*bytes* stay local.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

from . import config as config_mod

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# Serialises read-modify-write of the registry (re-entrant, mirrors savedruns.py).
_LOCK = threading.RLock()

# Extensions an entry's ``file`` may use — the same set :mod:`sounds` can play.
FILE_EXTS = (".wav", ".mp3")

# A sound id is a short slug: letters/digits/``-``/``_``. Kept tight so it embeds
# cleanly in a ``sound:<id>`` token and reads well as an agent-tool enum value.
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Sound:
    """A parsed ``[[sound]]`` entry."""

    id: str
    label: str = ""
    file: str = ""

    def exists(self) -> bool:
        """Whether the audio file is present on *this* machine (a synced entry
        whose file lives elsewhere resolves to False → silent no-op)."""
        try:
            return bool(self.file) and Path(self.file).is_file()
        except OSError:
            return False


def valid_id(sid: Any) -> bool:
    """Whether ``sid`` is a well-formed sound id (slug, ≤ 64 chars)."""
    return isinstance(sid, str) and bool(_ID_RE.match(sid))


def slugify(label: str) -> str:
    """Derive a candidate id from a label: lowercase, non-slug chars → ``-``.

    Pure; the caller de-duplicates against existing ids. Empty/garbage → ``sound``
    so there is always a non-empty base to disambiguate."""
    base = re.sub(r"[^a-z0-9]+", "-", str(label or "").strip().lower()).strip("-")
    return base[:64] or "sound"


def unique_id(label: str, existing: set[str]) -> str:
    """A slug id derived from ``label`` that is not in ``existing`` (append -2, -3, …)."""
    base = slugify(label)
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def parse_sound(data: dict[str, Any]) -> Sound:
    """Validate and parse one ``[[sound]]`` table.

    Raises ``ValueError`` with a human-first message on a missing/garbage id, a
    missing file, or a file whose extension the player can't handle."""
    sid = str(data.get("id") or "").strip()
    if not sid:
        raise ValueError("sound is missing an 'id'")
    if not valid_id(sid):
        raise ValueError(
            f"sound id '{sid}' is invalid — use letters, digits, '-' or '_' "
            "(max 64 chars)"
        )
    file = str(data.get("file") or "").strip()
    if not file:
        raise ValueError(f"sound '{sid}' is missing a 'file' path")
    if Path(file).suffix.lower() not in FILE_EXTS:
        raise ValueError(
            f"sound '{sid}' file must be a {' or '.join(FILE_EXTS)} (got '{file}')"
        )
    return Sound(id=sid, label=str(data.get("label") or "").strip(), file=file)


# ── registry I/O ──────────────────────────────────────────────────────────────


def load_rows() -> list[dict[str, Any]]:
    """All raw ``[[sound]]`` entries ([] when the file is absent/unreadable)."""
    path = config_mod.sounds_registry_path()
    with _LOCK:
        if not path.exists():
            return []
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            return []
        rows = data.get("sound")
        return [dict(r) for r in rows] if isinstance(rows, list) else []


def save_rows(rows: list[dict[str, Any]]) -> None:
    """Write the whole registry (atomic-ish: temp file + replace)."""
    path = config_mod.sounds_registry_path()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = tomli_w.dumps({"sound": rows})
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


def list_sounds() -> list[Sound]:
    """Every registered sound, skipping malformed entries (never raises)."""
    out: list[Sound] = []
    for row in load_rows():
        try:
            out.append(parse_sound(row))
        except ValueError:
            continue
    return out


def get_sound(sid: str) -> Sound | None:
    """One registered sound by id, or None."""
    for s in list_sounds():
        if s.id == sid:
            return s
    return None


def registry_map() -> dict[str, str]:
    """``{id: file}`` for every valid entry — the lookup :mod:`sounds` uses to
    resolve a ``sound:<id>`` token to a playable path. Never raises."""
    return {s.id: s.file for s in list_sounds()}

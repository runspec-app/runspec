"""active_room.py — the machine-local single-room selector.

A console may have several fleet hosts that each run a room server (a `room_port`
on the host entry). They all present the *same* logical helpdesk, so joining more
than one at once collides on the namespace `helpdesk/<queue>` and only one wins,
non-deterministically. Instead, this console picks **exactly one** room-configured
host to connect to — a per-console, never-synced choice (mirroring the active
*profile* pointer), so two consoles can't disagree over a shared flag and there's
no way to have two active at once.

The selection lives in `runspec_active_room.toml` (see `config.active_room_path`):

  * **absent** → not set yet: the *default* — auto-connect the sole room host if
    there's exactly one, else connect none (2+ hosts require an explicit pick).
  * **`active = ""`** → explicitly *no room* (deliberately detached).
  * **`active = "<host>"`** → connect only that host's room.

`read_active_room` returns `None` / `""` / `"<host>"` to keep those three states
distinct; `select_room_specs` applies them to the raw room-spec list. Both are
pure so the selection behaviour is unit-tested without a live console.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


def read_active_room(path: Path) -> str | None:
    """The stored selection: ``None`` (never set → default), ``""`` (explicit no
    room), or a host name. A missing/blank ``active`` key reads as ``None``; a
    present empty string reads as ``""`` (explicit none)."""
    if not path.exists():
        return None
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    val = data.get("active")
    if val is None:
        return None
    return str(val).strip()


def write_active_room(path: Path, host: str) -> None:
    """Persist the selection. ``""`` = explicit *no room*; a host name = that
    host only. (There is no way to write the ``None`` default — once the operator
    picks, the choice is explicit; the default only applies before first use.)"""
    path.write_text(f'active = "{str(host or "").strip()}"\n', encoding="utf-8")


def room_hosts(specs: list[dict[str, Any]]) -> list[str]:
    """Distinct host names that have a room spec, in first-seen order."""
    seen: list[str] = []
    for spec in specs:
        host = str(spec.get("host") or "")
        if host and host not in seen:
            seen.append(host)
    return seen


def select_room_specs(
    specs: list[dict[str, Any]], active: str | None
) -> list[dict[str, Any]]:
    """The room specs this console should actually join, given the selection.

    * ``active`` names a room host → only that host's specs.
    * ``active == ""`` (explicit none) → nothing.
    * ``active is None`` (unset) or a stale host no longer configured → the
      *default*: the sole room host if there's exactly one, else nothing (2+
      hosts require an explicit pick, so an upgrade never silently fans out).
    """
    hosts = room_hosts(specs)
    if active is not None:
        chosen = active.strip()
        if chosen == "":
            return []  # explicitly detached
        if chosen in hosts:
            return [s for s in specs if str(s.get("host") or "") == chosen]
        # A stale selection (host deleted / lost its room config) falls through
        # to the default rather than erroring or hard-disconnecting.
    if len(hosts) == 1:
        return list(specs)
    return []

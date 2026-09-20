"""self_service.py — read and write runspec_self_service.toml, the registry of
runnables offered to *self-service* visitors in the helpdesk room.

This replaces the runspec.toml `discoverable = ["self-service"]` marking. Whether
a runnable is self-service is a front-end concern — it means nothing to the
runnable as a CLI — so it lives here, in the console's own git-managed config
(shared across consoles via Config Sync), rather than in each package's
runspec.toml. The console discovers every visible runnable across the fleet; this
file picks the subset a visitor may request, and the catalog publisher tags those
entries `self-service` (everything else stays operator-only).

A self-service entry:

  [[self_service]]
  name        = "reset-password"   # the bare runnable name (the identity key)
  description = "Password resets"  # optional, shown in the manager UI

Minimal by design: it pins names (+ an optional description). An array-of-tables
so Config Sync merges it by `name` like the other collection files
(hosts/groups/filters/runbooks). The optional per-queue `room_catalog` allowlist
on a host entry still narrows this set per queue.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .tomlsafe import basic_string, load_table_array

logger = logging.getLogger(__name__)


def load_self_service(path: Path) -> list[dict[str, Any]]:
    """The ``[[self_service]]`` entries in ``path`` (``[]`` when the file is absent
    or malformed — a corrupt registry is quarantined, never fatal)."""
    return load_table_array(path, "self_service", log=logger)


def save_self_service(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write the registry. Entries are normalised to ``{name, description?}`` and
    de-duplicated by name (first wins, order preserved); blank names are dropped."""
    lines: list[str] = []
    for e in normalise_self_service(entries):
        lines.append("[[self_service]]")
        lines.append(f"name = {basic_string(e['name'])}")
        if e.get("description"):
            lines.append(f"description = {basic_string(e['description'])}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def normalise_self_service(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop blank names, de-duplicate by name (case-insensitive; first wins),
    preserving order. Each result is ``{"name": str, "description": str}``."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in entries or []:
        name = str(e.get("name") or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {"name": name, "description": str(e.get("description") or "").strip()}
        )
    return out


def self_service_names(entries: list[dict[str, Any]]) -> set[str]:
    """The set of self-service runnable names, normalised."""
    return {e["name"] for e in normalise_self_service(entries)}

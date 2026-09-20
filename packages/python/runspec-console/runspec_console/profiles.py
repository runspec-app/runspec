"""
profiles.py — read and write runspec_profiles.toml (the host-profile registry)
and the machine-local active-profile pointer.

A **profile** is a named *working set* of hosts: which remotes are in scope and
hold standing SSH connections right now. Exactly one profile is active at a time
(plus the built-in **All hosts** = no filter). Switching profiles is like
rebooting the console into a different host-scope — see
``docs/design/host-profiles.md``.

Two pieces, deliberately split (mirrors groups vs repo-paths):

* **The registry** (``runspec_profiles.toml``) — the controlled vocabulary of
  profile names, **shared** across consoles via Config Sync so the same profile
  means the same thing everywhere. A host's *membership* lives on its entry in
  ``runspec_hosts.toml`` (the ``profiles`` array), not here. Same array-of-tables
  shape as the group registry, so Config Sync merges it by ``name``::

      [[profile]]
      name        = "app-support"     # the profile name (unique, the identity key)
      description = "App-support fleet" # optional, shown in the manager UI

* **The active pointer** (``runspec_active_profile.toml``) — which profile *this
  machine* currently has loaded. Personal, moment-to-moment state, so it is
  **never** synced (not in Config Sync's KNOWN_FILES)::

      active = "app-support"   # "" / absent => All hosts (no filter)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from .tomlsafe import basic_string, load_table_array

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

logger = logging.getLogger(__name__)


def load_profiles(path: Path) -> list[dict[str, Any]]:
    """The ``[[profile]]`` entries in ``path`` (``[]`` when the file is absent or
    malformed — a corrupt registry is quarantined, never fatal)."""
    return load_table_array(path, "profile", log=logger)


def save_profiles(path: Path, profiles: list[dict[str, Any]]) -> None:
    """Write the registry. Entries are normalised to ``{name, description?}`` and
    de-duplicated by name (first wins, order preserved); blank names are dropped."""
    lines: list[str] = []
    for p in normalise_profiles(profiles):
        lines.append("[[profile]]")
        lines.append(f"name = {basic_string(p['name'])}")
        if p.get("description"):
            lines.append(f"description = {basic_string(p['description'])}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def normalise_profiles(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop blank names, de-duplicate by name (case-insensitive; first wins),
    preserving order. Each result is ``{"name": str, "description": str}``."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in profiles or []:
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {"name": name, "description": str(p.get("description") or "").strip()}
        )
    return out


def profile_names(profiles: list[dict[str, Any]]) -> list[str]:
    """Just the names, in registry order."""
    return [p["name"] for p in normalise_profiles(profiles)]


def read_active_profile(path: Path) -> str:
    """The machine-local active profile name (``""`` when absent/blank = All hosts)."""
    if not path.exists():
        return ""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    return str(data.get("active") or "").strip()


def write_active_profile(path: Path, name: str) -> None:
    """Persist the machine-local active profile name (``""`` clears = All hosts)."""
    path.write_text(f'active = "{str(name or "").strip()}"\n', encoding="utf-8")

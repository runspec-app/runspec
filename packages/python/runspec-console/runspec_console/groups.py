"""
groups.py — read and write runspec_groups.toml, the host-group registry.

A host's *membership* lives on its entry in ``runspec_hosts.toml`` (the ``group``
key). This file is the **canonical list of legal group names** — the controlled
vocabulary the host editor's group dropdown is populated from, shared across
consoles via Config Sync so a runbook/runbook targeting a group ("run X on the
Production group") resolves to the same group everywhere. Free-form group names
were a consistency hazard; this registry removes it.

A group entry:

  [[group]]
  name        = "Production"     # the group name (unique, the identity key)
  description = "Prod fleet"     # optional, shown in the manager UI

The registry is intentionally minimal: it pins names (+ an optional description),
not membership. It is an array-of-tables so Config Sync merges it by ``name`` like
the other collection files (hosts/filters/credentials/runbooks).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .tomlsafe import basic_string, load_table_array

logger = logging.getLogger(__name__)


def load_groups(path: Path) -> list[dict[str, Any]]:
    """The ``[[group]]`` entries in ``path`` (``[]`` when the file is absent or
    malformed — a corrupt registry is quarantined, never fatal)."""
    return load_table_array(path, "group", log=logger)


def save_groups(path: Path, groups: list[dict[str, Any]]) -> None:
    """Write the registry. Entries are normalised to ``{name, description?}`` and
    de-duplicated by name (first wins, order preserved); blank names are dropped."""
    lines: list[str] = []
    for g in normalise_groups(groups):
        lines.append("[[group]]")
        lines.append(f"name = {basic_string(g['name'])}")
        if g.get("description"):
            lines.append(f"description = {basic_string(g['description'])}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def normalise_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop blank names, de-duplicate by name (case-insensitive; first wins),
    preserving order. Each result is ``{"name": str, "description": str}``."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for g in groups or []:
        name = str(g.get("name") or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {"name": name, "description": str(g.get("description") or "").strip()}
        )
    return out


def group_names(groups: list[dict[str, Any]]) -> list[str]:
    """Just the names, in registry order."""
    return [g["name"] for g in normalise_groups(groups)]

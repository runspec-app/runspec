"""
filters_store.py — read and write runspec_filters.toml.

A filter is a named, reusable stream transform applied to a file *on the source
host* during a relay or a download (transfer pull). The pipeline is fed the file
on stdin and runs through the host shell, so only the matching output crosses the
network — handy for pulling a few relevant lines out of a gigabyte log.

A filter entry:

  [[filter]]
  name        = "id-lines"
  pipeline    = "grep -F -- {{arg}} | cut -d, -f1,4"
  description = "Lines containing the txn id, keep cols 1 & 4"
  param_label = "transaction id"

``{{arg}}`` is an optional placeholder substituted (shell-quoted) with a value the
operator supplies at transfer time; ``param_label`` labels that input in the UI.
``runspec_filters.toml`` lives alongside ``runspec_hosts.toml`` in the app dir.

A standalone ``[[filter]]`` table (rather than a ``[filters]`` section in
config.toml) is used because the config serialiser can't emit arrays of tables —
this mirrors the hosts file exactly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .tomlsafe import basic_string, load_table_array

logger = logging.getLogger(__name__)

# The fields persisted for a filter, in write order. Everything is a string;
# unknown keys are dropped on save so the file stays to schema.
_FIELDS = ("name", "pipeline", "description", "param_label")


def load_filters(path: Path) -> list[dict[str, Any]]:
    # A malformed file must never crash the app — this is read on the startup
    # path (Bridge.__init__ -> _reload_filters). Quarantine-and-continue.
    return load_table_array(path, "filter", log=logger)


def save_filters(path: Path, filters: list[dict[str, Any]]) -> None:
    lines: list[str] = []
    for f in filters:
        lines.append("[[filter]]")
        for k in _FIELDS:
            v = f.get(k)
            # Skip empty optional fields, but always write name + pipeline.
            if v is None or (v == "" and k not in ("name", "pipeline")):
                continue
            lines.append(f"{k} = {_toml_str(str(v))}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _toml_str(v: str) -> str:
    """Escape a string as a TOML basic string literal.

    Pipelines routinely contain ``"`` and ``\\`` (sed/awk), both of which need
    escaping for the file to read back byte-for-byte. Delegates to the shared
    audited serializer (also escapes control chars).
    """
    return basic_string(v)

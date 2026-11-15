"""Kill-switch blocks waiting for a host to reconnect.

The Denylist window's "Disable all" can only reach connected hosts. A host
that's disconnected at that moment is written here, and the console applies the
same block the next time it sees that host connected, then drops it from the
list. Machine-local (never synced): it's this console's promise to finish what
its operator started. Kept in ``{app_dir}/runspec_denylist_pending.json`` so it
survives a restart; the webview's storage doesn't.

Each entry: ``{"host": str, "reason": str, "since": float}``. Pure file I/O,
no SSH; the bridge applies the block (``manage_denylist``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

FILE_NAME = "runspec_denylist_pending.json"


def _path(app_dir: Path) -> Path:
    return app_dir / FILE_NAME


def read_pending(app_dir: Path) -> list[dict[str, Any]]:
    """Every pending block, oldest first. A missing or unreadable file is none."""
    try:
        data = json.loads(_path(app_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for e in data:
        if isinstance(e, dict) and isinstance(e.get("host"), str) and e["host"]:
            out.append(
                {
                    "host": e["host"],
                    "reason": str(e.get("reason") or ""),
                    "since": float(e.get("since") or 0),
                }
            )
    return out


def _write(app_dir: Path, entries: list[dict[str, Any]]) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    p = _path(app_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    tmp.replace(p)


def add_pending(
    app_dir: Path, hosts: list[str], reason: str = ""
) -> list[dict[str, Any]]:
    """Queue a block for each host (a host already queued keeps its first time,
    with the newer reason). Returns the whole list."""
    entries = read_pending(app_dir)
    by_host = {e["host"]: e for e in entries}
    now = time.time()
    for h in hosts:
        h = str(h or "").strip()
        if not h:
            continue
        if h in by_host:
            by_host[h]["reason"] = reason or by_host[h]["reason"]
        else:
            e = {"host": h, "reason": reason, "since": now}
            entries.append(e)
            by_host[h] = e
    _write(app_dir, entries)
    return entries


def remove_pending(app_dir: Path, host: str | None = None) -> list[dict[str, Any]]:
    """Drop one host's pending block, or all of them when ``host`` is None."""
    entries = (
        [] if host is None else [e for e in read_pending(app_dir) if e["host"] != host]
    )
    _write(app_dir, entries)
    return entries

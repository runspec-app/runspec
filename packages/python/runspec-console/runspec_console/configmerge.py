"""
configmerge.py — non-destructive merge of pulled config onto local config.

Config Sync used to overwrite each file wholesale. This module makes a pull
**top up** instead: it adds what's new, updates what matches, and leaves the
operator's own entries and machine-local keys alone. Two shapes:

* **Mappings** (``config.toml``, the triggers file's ``[rota]``) — a deep
  overlay: repo values land on top of local ones, local-only keys survive, and
  any *preserve* path keeps the local value (so e.g. ``[mailbox] folder`` is
  never clobbered).
* **Collections** (hosts / triggers / filters / credentials) — merge by each
  entry's natural identity (``name`` / ``id``): repo entries are added or update
  the matching local entry, local-only entries are kept, and a per-entry
  *preserve* field (e.g. ``room_user`` on every host) keeps the local value on a
  matched entry.

A *preserve* path is written ``"<file>:<dotted.path>"`` in
``[config_sync].preserve``. For a mapping file the path is the nested key
(``config.toml:mailbox.folder``); for a collection file the first segment is the
entry's key — or ``*`` for every entry — and the rest is the field within it
(``runspec_hosts.toml:*.room_user``). These are layered on top of
``DEFAULT_PRESERVE`` so the common machine-local keys work with no config.

All functions are pure (dict/list in, dict/list out) — no I/O — so they're unit
tested directly and the bridge owns reading/writing the files.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

# Machine-local keys protected out of the box; merged with the user's list.
DEFAULT_PRESERVE: tuple[str, ...] = (
    "config.toml:mailbox.folder",
    "runspec_hosts.toml:*.room_user",
)

_MISSING = object()


def parse_preserve(entries: Iterable[str]) -> dict[str, list[list[str]]]:
    """Group ``"<file>:<a.b.c>"`` preserve entries into ``{file: [[a, b, c], …]}``.

    Blank, separator-less, or empty-path entries are ignored. Duplicate paths
    per file are de-duplicated (order preserved)."""
    out: dict[str, list[list[str]]] = {}
    for raw in entries:
        text = str(raw).strip()
        if ":" not in text:
            continue
        file, _, path = text.partition(":")
        file = file.strip()
        segs = [s for s in path.strip().split(".") if s]
        if not file or not segs:
            continue
        bucket = out.setdefault(file, [])
        if segs not in bucket:
            bucket.append(segs)
    return out


def deep_overlay(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Recursively overlay ``over`` onto a copy of ``base`` (``over`` wins).

    Nested tables merge; scalars and lists (incl. arrays-of-tables) are replaced
    wholesale by ``over``. Keys only in ``base`` are kept (non-destructive)."""
    result = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_overlay(result[key], value)
        else:
            result[key] = value
    return result


def merge_mapping(
    local: dict[str, Any], incoming: dict[str, Any], preserve: list[list[str]]
) -> dict[str, Any]:
    """Deep-overlay ``incoming`` onto ``local``, then restore each preserve path
    to its local value (so the repo can't set a protected key)."""
    result = deep_overlay(local, incoming)
    for segs in preserve:
        _restore_path(result, local, segs)
    return result


def merge_collection(
    local: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    key: str,
    preserve: list[list[str]],
) -> list[dict[str, Any]]:
    """Merge two lists of entries by ``entry[key]``.

    Repo entries add (new key) or replace (matching key) local ones; local-only
    entries are kept in place. On a matched entry, preserve paths whose first
    segment is the entry's key (or ``*``) restore the local value of the field
    named by the remaining segments. Entries without a key are dropped."""
    merged: OrderedDict[str, dict[str, Any]] = OrderedDict()
    locals_by_key: dict[str, dict[str, Any]] = {}
    for entry in local:
        k = entry.get(key)
        if k is None:
            continue
        merged[str(k)] = dict(entry)
        locals_by_key[str(k)] = entry

    for entry in incoming:
        k = entry.get(key)
        if k is None:
            continue
        sk = str(k)
        new_entry = dict(entry)
        local_entry = locals_by_key.get(sk)
        if local_entry is not None:
            for segs in preserve:
                head, *rest = segs
                if rest and (head == "*" or head == sk):
                    _restore_path(new_entry, local_entry, rest)
        merged[sk] = new_entry
    return list(merged.values())


# ── layer folding (shared base config) ───────────────────────────────────────────

# Array-of-tables config files → (top-level key, per-entry identity field). These
# fold across layers by identity (merge_collection); everything else (config.toml,
# the triggers file's named [triggers.*] / [rota] tables) folds by deep_overlay.
COLLECTION_FILES: dict[str, tuple[str, str]] = {
    "runspec_hosts.toml": ("host", "name"),
    "runspec_filters.toml": ("filter", "name"),
    "runspec_credentials.toml": ("credential", "id"),
    "runspec_skills.toml": ("skill", "id"),
}


def fold_layers(name: str, parsed_layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold a file's parsed layers (low→high precedence) into one effective table.

    Collection files merge their array-of-tables by identity so a base layer's
    entries survive a later layer that only adds/updates a few; mapping files
    deep-overlay (later layer wins, earlier keys survive). The result has the
    file's native top-level shape, ready for the same per-file merge a single-layer
    pull uses. No *preserve* is applied here — that protects the local values and
    is applied once, downstream, against the local file."""
    coll = COLLECTION_FILES.get(name)
    if coll is not None:
        key_name, id_key = coll
        eff_list: list[dict[str, Any]] = []
        for parsed in parsed_layers:
            items = parsed.get(key_name) or []
            eff_list = merge_collection(eff_list, items, id_key, [])
        return {key_name: eff_list}
    eff: dict[str, Any] = {}
    for parsed in parsed_layers:
        eff = deep_overlay(eff, parsed)
    return eff


# ── path helpers ────────────────────────────────────────────────────────────────


def _get_path(node: Any, segs: list[str]) -> Any:
    cur = node
    for seg in segs:
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return _MISSING
    return cur


def _restore_path(
    target: dict[str, Any], source: dict[str, Any], segs: list[str]
) -> None:
    """Force ``target`` at ``segs`` to match ``source``: set it to source's value,
    or delete it when source has none (so a preserved-but-locally-unset key can't
    be injected from the repo)."""
    value = _get_path(source, segs)
    if value is _MISSING:
        _delete_path(target, segs)
    else:
        _set_path(target, segs, value)


def _set_path(target: dict[str, Any], segs: list[str], value: Any) -> None:
    cur = target
    for seg in segs[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    cur[segs[-1]] = value


def _delete_path(target: dict[str, Any], segs: list[str]) -> None:
    cur: Any = target
    for seg in segs[:-1]:
        if not isinstance(cur, dict) or seg not in cur:
            return
        cur = cur[seg]
    if isinstance(cur, dict):
        cur.pop(segs[-1], None)

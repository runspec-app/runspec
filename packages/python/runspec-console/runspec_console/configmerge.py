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

import re
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

# Machine-local keys protected out of the box; merged with the user's list.
DEFAULT_PRESERVE: tuple[str, ...] = (
    "config.toml:mailbox.folder",
    "config.toml:mailbox.folders",
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
    "runspec_groups.toml": ("group", "name"),
    "runspec_profiles.toml": ("profile", "name"),
    "runspec_filters.toml": ("filter", "name"),
    "runspec_credentials.toml": ("credential", "id"),
    "runspec_runbooks.toml": ("runbook", "id"),
    "runspec_saved_runs.toml": ("saved_run", "id"),
    "runspec_sounds.toml": ("sound", "id"),
    "runspec_repos.toml": ("repo", "name"),
    "runspec_self_service.toml": ("self_service", "name"),
    "runspec_evals.toml": ("suite", "name"),
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


# ── planned result (shared by the pull and its preview) ──────────────────────────
#
# A pull used to merge-and-write in one step, so there was nothing to show before
# accepting it. `merged_native` computes the post-pull table for one file from the
# local table + the folded incoming one, without I/O: the pull writes exactly
# this, and the preview diffs it against local — so what the operator reviews is
# what lands.

TRIGGERS_FILE = "runspec_triggers.toml"


def merged_native(
    name: str,
    local: dict[str, Any],
    incoming: dict[str, Any],
    preserve: list[list[str]],
) -> dict[str, Any]:
    """The table file ``name`` will hold after a pull, in its native shape.

    ``local`` is the current local table and ``incoming`` the folded repo table
    (both native-shaped). Collection files merge their array by identity (other
    top-level keys deep-overlay); the triggers file merges its named
    ``[triggers.*]`` tables by name and never takes the repo's ``[rota]`` (the
    rota is per-person and lives in its own file); mapping files
    (``config.toml``) deep-overlay. Pure."""
    coll = COLLECTION_FILES.get(name)
    if coll is not None:
        key_name, id_key = coll
        rest_local = {k: v for k, v in local.items() if k != key_name}
        rest_in = {k: v for k, v in incoming.items() if k != key_name}
        out = deep_overlay(rest_local, rest_in)
        out[key_name] = merge_collection(
            _entries(local.get(key_name)),
            _entries(incoming.get(key_name)),
            id_key,
            preserve,
        )
        return out
    if name == TRIGGERS_FILE:
        merged = merge_collection(
            _named_tables(local.get("triggers")),
            _named_tables(incoming.get("triggers")),
            "name",
            preserve,
        )
        triggers: dict[str, Any] = {}
        for trig in merged:
            body = dict(trig)
            triggers[str(body.pop("name"))] = body
        return {"triggers": triggers}
    return merge_mapping(local, incoming, preserve)


def _entries(raw: Any) -> list[dict[str, Any]]:
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def _named_tables(raw: Any) -> list[dict[str, Any]]:
    """``{name: {...}}`` tables → ``[{name, ...}]`` entries for an identity merge."""
    if not isinstance(raw, dict):
        return []
    return [{**val, "name": key} for key, val in raw.items() if isinstance(val, dict)]


def change_summary(
    name: str, before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    """What a pull changes in one file, for the preview's headline.

    Collection files (and the triggers file) report entry ids:
    ``{added, updated, removed, kept_local}`` where ``updated`` lists
    ``{id, fields}`` (the fields that change) and ``kept_local`` counts local-only
    entries the merge leaves alone. Mapping files report leaf keys:
    ``{added, changed, removed}`` as dotted paths. ``changed`` is True when
    anything differs at all. Pure."""
    coll = COLLECTION_FILES.get(name)
    if coll is not None or name == TRIGGERS_FILE:
        if coll is not None:
            key_name, id_key = coll
            b_list = _entries(before.get(key_name))
            a_list = _entries(after.get(key_name))
        else:
            key_name, id_key = "triggers", "name"
            b_list = _named_tables(before.get("triggers"))
            a_list = _named_tables(after.get("triggers"))
        b_by = {str(e.get(id_key)): e for e in b_list if e.get(id_key) is not None}
        a_by = {str(e.get(id_key)): e for e in a_list if e.get(id_key) is not None}
        added = [k for k in a_by if k not in b_by]
        removed = [k for k in b_by if k not in a_by]
        updated: list[dict[str, Any]] = []
        kept = 0
        for k, a in a_by.items():
            b = b_by.get(k)
            if b is None:
                continue
            fields = sorted(
                f for f in set(a) | set(b) if a.get(f, _MISSING) != b.get(f, _MISSING)
            )
            if fields:
                updated.append({"id": k, "fields": fields})
            else:
                kept += 1
        rest_changed = {k: v for k, v in before.items() if k != key_name} != {
            k: v for k, v in after.items() if k != key_name
        }
        return {
            "kind": "collection",
            "changed": bool(added or removed or updated or rest_changed),
            "added": added,
            "updated": updated,
            "removed": removed,
            "unchanged": kept,
        }
    b_leaves = {".".join(p): v for p, v in _iter_leaves(before, [])}
    a_leaves = {".".join(p): v for p, v in _iter_leaves(after, [])}
    added_keys = [k for k in a_leaves if k not in b_leaves]
    removed_keys = [k for k in b_leaves if k not in a_leaves]
    changed_keys = [k for k in a_leaves if k in b_leaves and a_leaves[k] != b_leaves[k]]
    return {
        "kind": "mapping",
        "changed": bool(added_keys or removed_keys or changed_keys),
        "added": added_keys,
        "updated": changed_keys,
        "removed": removed_keys,
    }


def render_diff(
    name: str,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    from_label: str = "local",
    to_label: str = "pulled",
) -> str:
    """A unified diff of ``before`` → ``after``, both rendered as normalised TOML.

    Rendering both sides the same way means the diff shows value changes only —
    not comment, ordering or quoting differences in the hand-edited local file.
    ``""`` when nothing differs."""
    import difflib

    a = _render_toml(name, before).splitlines(keepends=True)
    b = _render_toml(name, after).splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(a, b, f"{from_label}/{name}", f"{to_label}/{name}")
    )


def _render_toml(name: str, table: dict[str, Any]) -> str:
    """``table`` as diff-friendly TOML: one line per value.

    Every table and array-of-tables gets its own ``[a.b]`` / ``[[a.b]]`` header
    with its values one per line, so a changed field is a one-line change in the
    diff (``tomli_w`` packs small arrays of tables into inline lines, which
    would turn any edit to a host into a whole-entry change). Falls back to a
    sorted ``repr`` per key if a value can't be rendered. ``name`` is unused —
    kept so a file-specific rendering can be slotted in."""
    del name
    try:
        return "".join(_emit_table([], _drop_none(table), "")).lstrip("\n")
    except Exception:  # noqa: BLE001 - a preview must never fail on rendering
        return "".join(f"{k} = {table[k]!r}\n" for k in sorted(table))


def _emit_table(path: list[str], table: dict[str, Any], header: str) -> list[str]:
    """Lines for one table: its header (unless root), its plain values, then each
    sub-table / array-of-tables under its own header."""
    lines = [header] if header else []
    nested: list[str] = []
    for key, value in table.items():
        sub = path + [_toml_key(key)]
        if isinstance(value, dict) and value:
            nested.extend(_emit_table(sub, value, f"\n[{'.'.join(sub)}]\n"))
        elif _is_table_array(value):
            for entry in value:
                nested.extend(_emit_table(sub, entry, f"\n[[{'.'.join(sub)}]]\n"))
        else:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}\n")
    return lines + nested


def _is_table_array(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(v, dict) for v in value)
    )


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: Any) -> str:
    import json

    k = str(key)
    return k if _BARE_KEY.match(k) else json.dumps(k)


def _toml_value(value: Any) -> str:
    import tomli_w

    if isinstance(value, dict) and not value:
        return "{}"
    return tomli_w.dumps({"v": value})[len("v = ") :].rstrip("\n")


def _drop_none(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _drop_none(v) for k, v in node.items() if v is not None}
    if isinstance(node, list):
        return [_drop_none(v) for v in node if v is not None]
    return node


# ── override detection (seed/pull vs local) ──────────────────────────────────────
#
# A seed (or pull) is the lowest layer, but on a re-assert it overlays its values
# on top of local for every key it declares — so an operator who edited a
# seed-shipped key sees it silently reverted on the next package upgrade unless
# it's `preserve`d. `seed_overrides` surfaces exactly those keys ahead of time:
# where the incoming table and the local file hold a *different* value. Adds (a
# key local doesn't have yet) are not overrides and are omitted.


def seed_overrides(
    name: str,
    incoming: dict[str, Any],
    local: dict[str, Any],
    preserve: list[list[str]],
) -> list[dict[str, Any]]:
    """Leaf keys ``incoming`` would overwrite in ``local`` (both native-shaped).

    Returns ``[{file, path, seed, local, preserved}, …]`` — one per leaf the seed
    declares where local already holds a **different** value (values stringified +
    truncated). ``preserved`` is True when a ``preserve`` path pins the key local,
    so a re-assert would *not* actually override it (still surfaced, but flagged as
    protected). Collection files (hosts/credentials/…) diff by entry identity and
    report ``<id>.<field>``; mapping files report the dotted key. Pure."""
    out: list[dict[str, Any]] = []
    coll = COLLECTION_FILES.get(name)
    if coll is not None:
        key_name, id_key = coll
        local_by = {
            str(e.get(id_key)): e
            for e in (local.get(key_name) or [])
            if isinstance(e, dict) and e.get(id_key) is not None
        }
        for entry in incoming.get(key_name) or []:
            if not isinstance(entry, dict):
                continue
            k = entry.get(id_key)
            if k is None:
                continue
            sk = str(k)
            le = local_by.get(sk)
            if le is None:
                continue  # a new entry is an add, not an override
            for field, sval in entry.items():
                if field == id_key or field not in le:
                    continue  # identity, or a new field on an existing entry
                if le.get(field) != sval:
                    out.append(
                        _override_row(
                            name,
                            [sk, str(field)],
                            sval,
                            le.get(field),
                            _preserved_collection(sk, [str(field)], preserve),
                        )
                    )
    else:
        for segs, sval in _iter_leaves(incoming, []):
            lval = _get_path(local, segs)
            if lval is _MISSING or lval == sval:
                continue  # add, or unchanged
            out.append(
                _override_row(
                    name, segs, sval, lval, _preserved_mapping(segs, preserve)
                )
            )
    return out


def _iter_leaves(node: Any, prefix: list[str]):
    """Yield ``(segs, value)`` for every non-dict leaf under ``node`` (lists and
    scalars are leaves — ``deep_overlay`` replaces them wholesale)."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_leaves(v, prefix + [str(k)])
    else:
        yield prefix, node


def _preserved_mapping(segs: list[str], preserve: list[list[str]]) -> bool:
    """True when a preserve path is a prefix of (or equals) ``segs`` — preserving a
    table (``llm``) protects every leaf under it (``llm.api_key_command``)."""
    return any(segs[: len(p)] == p for p in preserve)


def _preserved_collection(
    key: str, field_segs: list[str], preserve: list[list[str]]
) -> bool:
    """True when a collection preserve entry (``<key|*>.<field…>``) pins this field."""
    for segs in preserve:
        head, *rest = segs
        if rest and (head == "*" or head == key) and field_segs[: len(rest)] == rest:
            return True
    return False


def _override_row(
    file: str, segs: list[str], seed_val: Any, local_val: Any, preserved: bool
) -> dict[str, Any]:
    return {
        "file": file,
        "path": ".".join(segs),
        "seed": _short(seed_val),
        "local": _short(local_val),
        "preserved": preserved,
    }


def _short(value: Any, limit: int = 200) -> str:
    """A compact display string for a config value (truncated)."""
    s = value if isinstance(value, str) else repr(value)
    s = str(s)
    return s if len(s) <= limit else s[: limit - 1] + "…"


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

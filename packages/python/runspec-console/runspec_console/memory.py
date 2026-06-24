"""
memory.py — persistent agent memory + curated runbooks (the cross-run knowledge
that makes repeat support queries cheaper).

Two stores, both small TOML collections in the app dir, both pure here (the bridge
owns when to read/write):

- **Memory** (`runspec_memory.toml`, machine-local, auto-accruing): facts the agent
  writes via the `remember` tool, keyed by an `app` tag. Only the *pinned* subset is
  folded into the system prompt (capped) so the prompt-cached prefix stays stable;
  the rest is fetched on demand via `search_memory` (stdlib token-overlap scoring —
  no embedding dependency). Never synced.
- **Skills** (`runspec_skills.toml`, shareable): operator/agent-authored runbooks
  (`symptom` → ordered `steps` naming which runnables to call). A `[[skill]]`
  collection so it rides the existing Config Sync (merged by `id`).

Writes are atomic-ish (temp file + replace) under a re-entrant lock, mirroring
``config.py``. Both files use ``tomli_w`` so arrays-of-tables / lists / bools / ints
round-trip without a hand-rolled serialiser.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tomli_w

from .config import memory_path, skills_path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# Serialises read-modify-write of both files (re-entrant so a public helper can call
# another while holding it).
_LOCK = threading.RLock()

_DEFAULT_APP = "general"
_WORD = re.compile(r"[a-z0-9]+")


# ── low-level collection I/O ──────────────────────────────────────────────────


def _load(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    rows = data.get(key)
    return [dict(r) for r in rows] if isinstance(rows, list) else []


def _save(path: Path, key: str, rows: list[dict[str, Any]]) -> None:
    text = tomli_w.dumps({key: rows})
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


# ── memory ────────────────────────────────────────────────────────────────────


def read_memory() -> list[dict[str, Any]]:
    """All memory entries ({}-safe). Public so the bridge / Config-Sync-preserve can
    read without importing internals."""
    with _LOCK:
        return _load(memory_path(), "memory")


def write_memory(rows: list[dict[str, Any]]) -> None:
    with _LOCK:
        _save(memory_path(), "memory", rows)


def add_memory(
    fact: str,
    *,
    app: str = _DEFAULT_APP,
    tags: list[str] | tuple[str, ...] = (),
    pin: bool = False,
    cap: int = 50,
) -> str:
    """Append a fact and return its id. Evicts the app's oldest unpinned entries
    once it exceeds ``cap``. A blank fact is rejected (returns "")."""
    fact = (fact or "").strip()
    if not fact:
        return ""
    app = (app or _DEFAULT_APP).strip() or _DEFAULT_APP
    entry = {
        "id": "m_" + uuid.uuid4().hex[:8],
        "app": app,
        "fact": fact,
        "tags": [str(t).strip() for t in tags if str(t).strip()],
        "pinned": bool(pin),
        "created": _now(),
        "hits": 0,
    }
    with _LOCK:
        rows = _load(memory_path(), "memory")
        rows.append(entry)
        rows = _evict(rows, app, cap)
        _save(memory_path(), "memory", rows)
    return entry["id"]


def _evict(rows: list[dict[str, Any]], app: str, cap: int) -> list[dict[str, Any]]:
    """Keep at most ``cap`` entries for ``app``: pinned always survive; among the
    rest keep the most-used then newest. Other apps are untouched."""
    in_app = [r for r in rows if r.get("app") == app]
    if len(in_app) <= cap:
        return rows
    pinned = [r for r in in_app if r.get("pinned")]
    rest = [r for r in in_app if not r.get("pinned")]
    rest.sort(
        key=lambda r: (int(r.get("hits", 0)), str(r.get("created", ""))), reverse=True
    )
    keep = pinned + rest[: max(0, cap - len(pinned))]
    # Preserve original order for stable output / diffs.
    keep_ids = {r["id"] for r in keep if r.get("id")}
    return [r for r in rows if r.get("app") != app or r.get("id") in keep_ids]


def search_memory(
    query: str, *, app: str | None = None, k: int = 5
) -> list[dict[str, Any]]:
    """Top-``k`` entries ranked by token overlap with ``query`` (optionally scoped to
    one ``app``). Increments ``hits`` on the returned entries (persisted). Returns the
    matched entries (id/app/fact/tags), most relevant first."""
    q = _tokens(query)
    with _LOCK:
        rows = _load(memory_path(), "memory")
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for r in rows:
            if app and r.get("app") != app:
                continue
            hay = _tokens(str(r.get("fact", "")) + " " + " ".join(r.get("tags", [])))
            score = len(q & hay)
            if score:
                scored.append((score, str(r.get("created", "")), r))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        top = [r for _s, _c, r in scored[: max(0, k)]]
        if top:
            hit_ids = {r["id"] for r in top if r.get("id")}
            for r in rows:
                if r.get("id") in hit_ids:
                    r["hits"] = int(r.get("hits", 0)) + 1
            _save(memory_path(), "memory", rows)
        return [
            {
                "id": r.get("id"),
                "app": r.get("app"),
                "fact": r.get("fact"),
                "tags": r.get("tags", []),
            }
            for r in top
        ]


def build_header(max_facts: int = 6, max_chars: int = 700) -> str:
    """The small block folded into the system prompt: the **pinned** facts only
    (most-used first), as ``- [app] fact`` bullets, hard-capped by count and chars.

    Returns ``""`` when nothing is pinned, so a session with no pins leaves the
    cached system prefix byte-identical (no prompt-cache churn)."""
    with _LOCK:
        rows = _load(memory_path(), "memory")
    pinned = [r for r in rows if r.get("pinned")]
    if not pinned:
        return ""
    pinned.sort(key=lambda r: int(r.get("hits", 0)), reverse=True)
    lines = ["## Known facts (memory)"]
    for r in pinned[: max(0, max_facts)]:
        line = f"- [{r.get('app', _DEFAULT_APP)}] {str(r.get('fact', '')).strip()}"
        if len("\n".join(lines)) + len(line) + 1 > max_chars:
            break
        lines.append(line)
    return "\n".join(lines) if len(lines) > 1 else ""


# ── skills (curated runbooks) ───────────────────────────────────────────────────


def load_skills() -> list[dict[str, Any]]:
    """All skill entries ({}-safe). Used by the bridge and the Config-Sync merge."""
    with _LOCK:
        return _load(skills_path(), "skill")


def save_skills(rows: list[dict[str, Any]]) -> None:
    with _LOCK:
        _save(skills_path(), "skill", rows)


def list_skills() -> list[dict[str, Any]]:
    """Skill metadata for the agent's `load_skill` enum + display: id/title/symptom."""
    return [
        {
            "id": s.get("id"),
            "title": s.get("title", ""),
            "symptom": s.get("symptom", ""),
        }
        for s in load_skills()
        if s.get("id")
    ]


def load_skill(skill_id: str) -> dict[str, Any] | None:
    """One full skill (title/symptom/steps/tags/args) by id, or None."""
    return next((s for s in load_skills() if s.get("id") == skill_id), None)


def save_skill(
    skill_id: str,
    *,
    title: str,
    symptom: str = "",
    steps: str = "",
    tags: list[str] | tuple[str, ...] = (),
    args: dict[str, Any] | None = None,
) -> str:
    """Create or replace a runbook by id (slugified from title when blank). Returns
    the id. ``steps`` is the body/prompt template (free text naming which runnables
    to call, with ``{{name}}`` placeholders that map to ``args``). A step may name a
    **host group** instead of a single host (e.g. "run restart-service on the
    Production group") — the agent passes the group name as ``run_runnable``'s host
    to fan the run out across every member behind one grouped confirm. ``args`` is
    an optional ``[skill.args]`` table using the exact runspec arg grammar — see
    ``skillargs.py`` for how it is parsed/validated/rendered (the same core machinery
    runnable forms use)."""
    skill_id = (skill_id or "").strip() or _slug(title)
    if not skill_id:
        return ""
    entry: dict[str, Any] = {
        "id": skill_id,
        "title": (title or "").strip(),
        "symptom": (symptom or "").strip(),
        "steps": (steps or "").strip(),
        "tags": [str(t).strip() for t in tags if str(t).strip()],
        "updated": _now(),
    }
    # Only persist a non-empty args table — keeps param-less skills clean and the
    # TOML free of empty `[skill.args]` tables.
    if args:
        entry["args"] = dict(args)
    with _LOCK:
        rows = _load(skills_path(), "skill")
        rows = [s for s in rows if s.get("id") != skill_id]
        rows.append(entry)
        _save(skills_path(), "skill", rows)
    return skill_id


# ── body rendering ──────────────────────────────────────────────────────────────

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_-]+)\s*\}\}")


def render_skill_body(steps: str, values: dict[str, Any]) -> str:
    """Substitute ``{{name}}`` placeholders in a skill body with ``values`` (the
    operator's form input). Unknown placeholders are left intact so the agent can see
    an unfilled slot rather than a silent blank. Mirrors the ``{{arg}}`` convention of
    ``fs_transfer.render_pipeline`` (no shell-quoting here — this is prompt text)."""

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in values:
            return m.group(0)
        v = values[name]
        if isinstance(v, bool):
            return "true" if v else "false"
        return "" if v is None else str(v)

    return _PLACEHOLDER.sub(_sub, steps or "")


def _slug(text: str) -> str:
    return "-".join(_WORD.findall((text or "").lower()))[:48]

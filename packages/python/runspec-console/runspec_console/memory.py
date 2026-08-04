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
- **Runbooks** (`runspec_runbooks.toml`, shareable): operator/agent-authored runbooks
  (`symptom` → ordered steps naming which runnables to call). A `[[runbook]]`
  collection so it rides the existing Config Sync (merged by `id`). Steps are stored
  as an ordered `[[runbook.step]]` array-of-tables (`text` + optional `title`), so the
  file stays readable/diffable; a legacy single `steps` string is still read. Either
  form renders to numbered prose via `render_runbook` — execution is prose-only.

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

from .config import memory_path, runbooks_path

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
    # multiline_strings=True renders any newline-containing string as a triple-quoted
    # """…""" literal instead of one escaped line — so a multi-line runbook step `text`
    # (or a memory fact / legacy `steps` body) stays readable and diffable in the file
    # and in an external editor. Single-line strings are unaffected and it round-trips
    # identically on read.
    text = tomli_w.dumps({key: rows}, multiline_strings=True)
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


#: Default count of pinned facts that fold into the system prompt (build_header).
HEADER_MAX_FACTS = 6


def pinned_count() -> int:
    """How many memory facts are pinned (fold into the system prompt). Surfaced by
    the context indicator so the operator can spot a bloated pin set — only the top
    ``HEADER_MAX_FACTS`` actually reach the prompt."""
    with _LOCK:
        rows = _load(memory_path(), "memory")
    return sum(1 for r in rows if r.get("pinned"))


def build_header(max_facts: int = HEADER_MAX_FACTS, max_chars: int = 700) -> str:
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


# ── runbooks (curated runbooks) ───────────────────────────────────────────────────


def load_runbooks() -> list[dict[str, Any]]:
    """All runbook entries ({}-safe). Used by the bridge and the Config-Sync merge."""
    with _LOCK:
        return _load(runbooks_path(), "runbook")


def save_runbooks(rows: list[dict[str, Any]]) -> None:
    with _LOCK:
        _save(runbooks_path(), "runbook", rows)


def list_runbooks() -> list[dict[str, Any]]:
    """Runbook metadata for the agent's `load_runbook` enum + display: id/title/symptom.

    **Sorted by id**, which is a prompt-cache guarantee, not cosmetics. This feeds
    the agent tool schemas, and those sit before the cache breakpoint. ``save_runbook``
    replaces an entry by delete-then-append, so it moves the edited runbook to the end
    of the file — in file order, *any* re-save (even a steps-only edit that changes
    nothing the agent can see) reorders the enum, changes the tools block byte-for-byte,
    and invalidates the cached prefix for the rest of the session. Sorting makes the
    schemas depend on the *set* of runbooks rather than the order they were last
    touched. Storage order is untouched; ``load_runbooks`` still returns the file as-is."""
    return sorted(
        (
            {
                "id": s.get("id"),
                "title": s.get("title", ""),
                "symptom": s.get("symptom", ""),
            }
            for s in load_runbooks()
            if s.get("id")
        ),
        key=lambda r: str(r["id"]),
    )


def load_runbook(runbook_id: str) -> dict[str, Any] | None:
    """One full runbook (title/symptom/steps/tags/args) by id, or None."""
    return next((s for s in load_runbooks() if s.get("id") == runbook_id), None)


def delete_runbook(runbook_id: str) -> bool:
    """Remove one runbook by id via read-modify-write (the whole file is re-read, the
    matching entry dropped, and the rest written back untouched) — so deleting one
    runbook never disturbs the others, including any edited on disk since load. Returns
    True if an entry was removed."""
    rid = str(runbook_id or "").strip()
    if not rid:
        return False
    with _LOCK:
        rows = _load(runbooks_path(), "runbook")
        kept = [s for s in rows if s.get("id") != rid]
        if len(kept) == len(rows):
            return False
        _save(runbooks_path(), "runbook", kept)
    return True


def save_runbook(
    runbook_id: str,
    *,
    title: str,
    symptom: str = "",
    steps: str | list[dict[str, Any]] | None = "",
    tags: list[str] | tuple[str, ...] = (),
    args: dict[str, Any] | None = None,
    discoverable: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Create or replace a runbook by id (slugified from title when blank). Returns
    the id. ``steps`` is the body/prompt the agent follows (free text naming which
    runnables to call, with ``{{name}}`` placeholders that map to ``args``). It is
    stored one of two ways:

    - a **list** of ``{"text": ..., "title"?: ...}`` rows → persisted as an ordered
      ``[[runbook.step]]`` array-of-tables (the readable, diffable form), and the
      legacy ``steps`` string key is dropped;
    - a **string** → persisted as the legacy ``steps`` key unchanged (so agent
      free-text authors and existing seeds are undisturbed — auto-splitting prose
      would risk lossy re-numbering).

    A step may name a **host group** instead of a single host (e.g. "run
    restart-service on the Production group") — the agent passes the group name as
    ``run_runnable``'s host to fan the run out across every member behind one grouped
    confirm. ``args`` is an optional ``[runbook.args]`` table using the exact runspec
    arg grammar — see ``runbookargs.py`` for how it is parsed/validated/rendered (the
    same core machinery runnable forms use). Either way ``render_runbook`` turns the
    stored steps back into numbered prose at run time."""
    runbook_id = (runbook_id or "").strip() or _slug(title)
    if not runbook_id:
        return ""
    entry: dict[str, Any] = {
        "id": runbook_id,
        "title": (title or "").strip(),
        "symptom": (symptom or "").strip(),
        "tags": [str(t).strip() for t in tags if str(t).strip()],
        "updated": _now(),
    }
    # Structured rows → the ``[[runbook.step]]`` array (no legacy string key); a
    # string → the legacy ``steps`` key. Because the entry is rebuilt fresh and
    # replaces the old one by id, whichever key we omit is naturally dropped.
    if isinstance(steps, (list, tuple)):
        entry["step"] = _coerce_steps(steps)
    else:
        entry["steps"] = (steps or "").strip()
    # Only persist a non-empty args table — keeps param-less runbooks clean and the
    # TOML free of empty `[runbook.args]` tables.
    if args:
        entry["args"] = dict(args)
    # The publish audience (``discoverable``) — which queue catalogs this runbook is
    # offered in (e.g. ``["operator"]``). Opt-in: only persisted when non-empty, so an
    # ordinary runbook stays out of every catalog. Mirrors a runnable's H0 field.
    aud = [str(a).strip() for a in (discoverable or []) if str(a).strip()]
    if aud:
        entry["discoverable"] = aud
    with _LOCK:
        rows = _load(runbooks_path(), "runbook")
        rows = [s for s in rows if s.get("id") != runbook_id]
        rows.append(entry)
        _save(runbooks_path(), "runbook", rows)
    return runbook_id


# ── steps: structured ⇄ legacy string ────────────────────────────────────────────

# Splits a legacy free-text body into rows on lines that begin with a step number
# ("1. ", "2) ", …). Editor convenience only — never used by rendering.
_STEP_LINE = re.compile(r"^\s*\d+[.)]\s+(.*)$")


def _coerce_steps(steps: Any) -> list[dict[str, str]]:
    """Clean a list of step rows → ``[{"text": ..., "title"?: ...}]``, dropping rows
    with no ``text``. Only ``title``/``text`` are carried — no per-step machine field
    is ever stored (that would cross the "no workflow engine" cliff)."""
    rows: list[dict[str, str]] = []
    for item in steps or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        row: dict[str, str] = {"text": text}
        title = str(item.get("title") or "").strip()
        if title:
            row["title"] = title
        rows.append(row)
    return rows


def normalize_runbook_steps(entry: dict[str, Any]) -> list[dict[str, str]]:
    """A runbook's steps as canonical rows for the editor UI — ``[{"text", "title"?}]``.

    From the ``[[runbook.step]]`` array when present; otherwise a best-effort split of
    the legacy ``steps`` string on leading step-number lines (falling back to a single
    row when it isn't numbered). Display/edit convenience only — rendering uses
    ``render_runbook`` so legacy prose fidelity is untouched."""
    step = entry.get("step")
    if isinstance(step, list) and step:
        return _coerce_steps(step)
    body = str(entry.get("steps") or "").strip()
    if not body:
        return []
    rows: list[dict[str, str]] = []
    for line in body.splitlines():
        m = _STEP_LINE.match(line)
        if m:
            rows.append({"text": m.group(1).strip()})
        elif rows:
            # A continuation line of the current step (preserve intra-step newlines).
            rows[-1]["text"] = (rows[-1]["text"] + "\n" + line.strip()).strip()
        else:
            rows.append({"text": line.strip()})
    # No numbered lines at all → keep the body as one step rather than one row per line.
    if not any(_STEP_LINE.match(line) for line in body.splitlines()):
        return [{"text": body}]
    return [r for r in rows if r["text"]]


def _steps_to_prose(step: list[dict[str, Any]]) -> str:
    """Join a ``[[runbook.step]]`` array into the numbered prose the agent follows:
    ``"1. text"`` per step, or ``"1. Title: text"`` when a step has a ``title``."""
    lines: list[str] = []
    n = 0
    for item in step:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        n += 1
        title = str(item.get("title") or "").strip()
        lines.append(f"{n}. {title}: {text}" if title else f"{n}. {text}")
    return "\n".join(lines)


# ── body rendering ──────────────────────────────────────────────────────────────

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_-]+)\s*\}\}")


def render_runbook_body(steps: str, values: dict[str, Any]) -> str:
    """Substitute ``{{name}}`` placeholders in a runbook body with ``values`` (the
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


def render_runbook(entry: dict[str, Any], values: dict[str, Any]) -> str:
    """Render a runbook's body to the numbered prose handed to the agent turn.

    Structured ``[[runbook.step]]`` array → joined to ``"1. …\\n2. …"`` then run
    through ``render_runbook_body`` for ``{{placeholder}}`` substitution. A legacy
    ``steps`` string → ``render_runbook_body`` directly (byte-identical to before, no
    renumbering). Execution stays prose-only either way — this is the sole place a
    step array becomes anything other than text."""
    step = entry.get("step")
    if isinstance(step, list) and step:
        return render_runbook_body(_steps_to_prose(step), values)
    return render_runbook_body(str(entry.get("steps") or ""), values)


def _slug(text: str) -> str:
    return "-".join(_WORD.findall((text or "").lower()))[:48]

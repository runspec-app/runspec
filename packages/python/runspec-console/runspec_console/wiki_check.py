"""The wiki check: what in the team wiki needs a look.

Reads the team's pages (``wiki_pages``) against the runnables the console has
discovered and returns findings, each one specific enough to act on:

- ``clash``: a runnable's page mentions a ``--flag`` its ``runspec.toml`` doesn't
  have (on the runnable or any of its verbs), so the page is out of date or
  wrong.
- ``stale``: a page whose ``stale_after`` date has passed, or that is marked
  ``deprecated`` while the tool is still installed.
- ``duplicate``: the same bullet on two pages (or twice on one), which drifts
  apart over time.
- ``gap``: installed runnables with no page yet.

Read-only and pure (stdlib); the Knowledge tab and the agent's ``check_wiki``
tool both use it.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable

from . import wiki_pages

#: Flags every runspec runnable understands, never a clash.
COMMON_FLAGS = frozenset({"help", "debug", "version", "env", "run-as", "become-method"})
#: Most findings of one kind returned (the summary still counts them all).
MAX_PER_KIND = 25

_FLAG = re.compile(r"(?<![\w-])--([a-z0-9][a-z0-9_-]*)", re.IGNORECASE)


def _norm_flag(name: str) -> str:
    return str(name).strip().lower().replace("_", "-")


def known_flags(records: Iterable[dict[str, Any]]) -> dict[str, set[str]]:
    """``{runnable name: {arg names as flags}}`` over every discovered record of
    that name (all hosts and venvs) and all of its verbs."""
    out: dict[str, set[str]] = {}

    def walk(rec: dict[str, Any], into: set[str]) -> None:
        for a in rec.get("args") or []:
            if isinstance(a, dict) and a.get("name"):
                into.add(_norm_flag(a["name"]))
        for sub in (rec.get("commands") or {}).values():
            if isinstance(sub, dict):
                walk(sub, into)

    for rec in records:
        name = str(rec.get("name") or "")
        if name:
            walk(rec, out.setdefault(name, set()))
    return out


def page_flags(page: dict[str, Any]) -> list[str]:
    """The ``--flags`` a page mentions, in order, once each."""
    seen: list[str] = []
    for s in page.get("sections") or []:
        for m in _FLAG.finditer(str(s.get("text") or "")):
            flag = _norm_flag(m.group(1))
            if flag not in seen:
                seen.append(flag)
    return seen


def check(
    pages: list[dict[str, Any]],
    records: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """``{summary: {clash, stale, duplicate, gap}, findings: [...]}``. Each
    finding is ``{kind, entity, path, message}`` (plus ``flags`` for a clash,
    ``paths`` for a duplicate, ``names`` for the gap)."""
    today = today or date.today()
    flags = known_flags(records)
    installed = set(flags)
    findings: dict[str, list[dict[str, Any]]] = {
        "clash": [],
        "stale": [],
        "duplicate": [],
        "gap": [],
    }

    by_entity = {p.get("entity"): p for p in pages if p.get("entity")}
    for page in pages:
        entity = str(page.get("entity") or "")
        path = str(page.get("path") or "")
        kind, _, name = entity.partition(":")

        if kind == "runnable" and name in installed:
            unknown = [
                f
                for f in page_flags(page)
                if f not in flags[name] and f not in COMMON_FLAGS
            ]
            if unknown:
                findings["clash"].append(
                    {
                        "kind": "clash",
                        "entity": entity,
                        "path": path,
                        "flags": unknown,
                        "message": (
                            f"{path} mentions "
                            + ", ".join(f"--{f}" for f in unknown)
                            + f", which {name}'s runspec.toml doesn't have."
                        ),
                    }
                )

        stale_after = str(page.get("stale_after") or "")
        if stale_after:
            try:
                due = date.fromisoformat(stale_after[:10])
            except ValueError:
                due = None
            if due is not None and due < today:
                findings["stale"].append(
                    {
                        "kind": "stale",
                        "entity": entity,
                        "path": path,
                        "message": f"{path} was due to be checked again on {stale_after[:10]}.",
                    }
                )
        if (
            str(page.get("status") or "").lower() == "deprecated"
            and kind == "runnable"
            and name in installed
        ):
            findings["stale"].append(
                {
                    "kind": "stale",
                    "entity": entity,
                    "path": path,
                    "message": f"{path} is marked deprecated, but {name} is still installed.",
                }
            )

    # The same bullet in more than one place.
    where: dict[str, list[str]] = {}
    text_of: dict[str, str] = {}
    for page in pages:
        for s in page.get("sections") or []:
            for b in s.get("bullets") or []:
                key = " ".join(wiki_pages.plain(b).lower().split())
                if len(key) < 24:  # short bullets repeat legitimately
                    continue
                where.setdefault(key, []).append(str(page.get("path") or ""))
                text_of.setdefault(key, wiki_pages.plain(b))
    for key, paths in where.items():
        if len(paths) > 1:
            uniq = sorted(set(paths))
            findings["duplicate"].append(
                {
                    "kind": "duplicate",
                    "entity": "",
                    "path": uniq[0],
                    "paths": uniq,
                    "message": (
                        f"“{text_of[key][:120]}” is on "
                        + (" and ".join(uniq) if len(uniq) > 1 else f"{uniq[0]} twice")
                        + "."
                    ),
                }
            )

    missing = sorted(n for n in installed if f"runnable:{n}" not in by_entity)
    if missing:
        findings["gap"].append(
            {
                "kind": "gap",
                "entity": "",
                "path": "",
                "names": missing,
                "message": (
                    f"{len(missing)} installed tool{'s' if len(missing) != 1 else ''} "
                    f"{'have' if len(missing) != 1 else 'has'} no page: "
                    + ", ".join(missing[:8])
                    + ("…" if len(missing) > 8 else "")
                    + "."
                ),
            }
        )

    summary = {
        "clash": len(findings["clash"]),
        "stale": len(findings["stale"]),
        "duplicate": len(findings["duplicate"]),
        "gap": len(missing),
    }
    flat: list[dict[str, Any]] = []
    for kind in ("clash", "stale", "duplicate", "gap"):
        flat += findings[kind][:MAX_PER_KIND]
    return {"summary": summary, "findings": flat}

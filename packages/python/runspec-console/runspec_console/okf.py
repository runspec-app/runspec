"""The team wiki as an Open Knowledge Format (OKF v0.2) bundle.

A wiki repo is a folder of Markdown pages, one per thing (a runnable, a service,
a term), each with YAML frontmatter whose ``type`` says what it is. This module
builds the starter repo shipped in ``wiki_template/`` and the ``runspec/`` pages
in it, which are generated from the console's documentation summaries
(``knowledge_seed.toml``) rather than written by hand.

Pure and stdlib-only. The lint lives in ``wiki_template/tools/okf_lint.py`` so
the same file runs in the console and in a wiki repo's CI; :func:`lint_files`
loads it from there.

Generated pages carry ``generated: {by: runspec-console-seed/<hash>, at: <date>}``.
The hash covers the page's content, so a page whose facts didn't change keeps
its old date and a refresh only touches pages that really changed.

Regenerate the committed pages after editing the seed::

    python -m runspec_console.okf --write     # or --check in CI
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import importlib.util
import json
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

TEMPLATE_DIR = Path(__file__).with_name("wiki_template")
SEED_PATH = Path(__file__).with_name("knowledge_seed.toml")
#: Where generated pages live in a wiki repo.
GENERATED_DIR = "runspec"
#: The branch a refresh pushes to, for review like any other change.
REFRESH_BRANCH = "refresh/runspec-docs"
#: Where Create puts the template when the team branch already exists (a repo
#: made with a README, or a default branch the host created), for a merge request.
SETUP_BRANCH = "setup/wiki-template"
OKF_VERSION = "0.2"
PRODUCER = "runspec-console-seed"

#: Entity type → (folder, OKF ``type``, index heading).
ENTITY_PAGES: dict[str, tuple[str, str, str]] = {
    "app": ("apps", "Application", "Applications"),
    "feature": ("features", "Feature", "Features"),
    "topic": ("topics", "Topic", "Topics"),
    "runnable": ("runnables", "Runnable", "Runnables"),
    "service": ("services", "Service", "Services"),
    "host": ("hosts", "Host", "Hosts"),
    "group": ("hosts", "Host Group", "Hosts"),
    "profile": ("hosts", "Host Profile", "Hosts"),
    "term": ("terms", "Term", "Terms"),
    "team": ("teams", "Team", "Teams"),
    "runbook": ("runbooks", "Playbook", "Runbooks"),
    "trigger": ("automation", "Trigger", "Automation"),
    "schedule": ("automation", "Schedule", "Automation"),
    "file": ("files", "File", "Files"),
}
#: Fact kind → the page section it lands in, in page order.
SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("What it is", ("glossary",)),
    ("How to", ("procedure",)),
    ("Notes", ("note", "location", "effect", "pairing", "host", "arg-habit")),
)

_URL = re.compile(r"https?://[^\s)\]>\"']+")
_SLUG = re.compile(r"[^a-z0-9]+")


# ── entities and paths ──────────────────────────────────────────────────────


def _split(ref: str) -> tuple[str, str]:
    kind, _, name = ref.partition(":")
    return (kind.strip().lower(), name.strip()) if name else ("topic", ref.strip())


def slug(name: str) -> str:
    return _SLUG.sub("-", name.lower()).strip("-") or "page"


def page_path(ref: str) -> str:
    """``"feature:mcp server"`` → ``"runspec/features/mcp-server.md"``."""
    kind, name = _split(ref)
    folder = ENTITY_PAGES.get(kind, ENTITY_PAGES["topic"])[0]
    return f"{GENERATED_DIR}/{folder}/{slug(name)}.md"


def _title(ref: str) -> str:
    return _split(ref)[1]


def _okf_type(ref: str) -> str:
    return ENTITY_PAGES.get(_split(ref)[0], ENTITY_PAGES["topic"])[1]


def _primary(about: list[str]) -> str:
    """The page a fact lives on: its most specific entity. Seed facts list the
    app first and then what they're about, so that's the first non-app entity
    (the app itself when there's nothing else)."""
    for ref in about:
        if _split(ref)[0] != "app":
            return ref
    return about[0]


def _q(value: str) -> str:
    """A YAML double-quoted scalar (JSON strings are valid YAML)."""
    return json.dumps(value, ensure_ascii=False)


def _first_sentence(text: str, limit: int = 200) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    out = m.group(1) if m else text
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


# ── seed → pages ────────────────────────────────────────────────────────────


def load_seed(path: Path = SEED_PATH) -> list[dict[str, Any]]:
    """The seed's ``[[fact]]`` rows in file order, ``about`` order kept (the
    knowledge store sorts it, which loses which entity a fact is mainly about)."""
    with path.open("rb") as fh:
        rows = tomllib.load(fh).get("fact") or []
    out = []
    for row in rows:
        about = [str(a).strip() for a in row.get("about") or [] if str(a).strip()]
        text = " ".join(str(row.get("text") or "").split())
        if about and text:
            out.append(
                {"kind": str(row.get("kind") or "note"), "about": about, "text": text}
            )
    return out


def _page_body(ref: str, facts: list[dict[str, Any]], related: list[str]) -> str:
    lines = [f"# {_title(ref)}", ""]
    for heading, kinds in SECTIONS:
        rows = [f for f in facts if f["kind"] in kinds]
        if not rows:
            continue
        lines += [f"## {heading}", ""]
        for f in rows:
            lines += [f["text"], ""]
    if not facts:
        lines += ["No notes of its own yet; see the related pages below.", ""]
    if related:
        lines += ["## See also", ""]
        for other in related:
            lines.append(f"- [{_title(other)}](/{page_path(other)})")
        lines.append("")
    return "\n".join(lines)


def _frontmatter(ref: str, facts: list[dict[str, Any]], by: str, at: str) -> str:
    urls: list[str] = []
    for f in facts:
        for url in _URL.findall(f["text"]):
            url = url.rstrip(".,;:")
            if url not in urls:
                urls.append(url)
    desc = (
        _first_sentence(facts[0]["text"])
        if facts
        else f"Pages related to {_title(ref)}."
    )
    kind = _split(ref)[0]
    lines = [
        "---",
        f"type: {_q(_okf_type(ref))}",
        f"title: {_q(_title(ref))}",
        f"description: {_q(desc)}",
        f"tags: [runspec, {kind}]",
        "status: stable",
    ]
    if urls:
        lines.append("sources:")
        for i, url in enumerate(urls, start=1):
            lines += [f"  - id: S{i}", f"    resource: {_q(url)}"]
    lines += ["generated:", f"  by: {by}", f"  at: {_q(at)}", "---", ""]
    return "\n".join(lines)


def _existing_at(text: str) -> str | None:
    m = re.search(
        r"^generated:\n  by: (.*)\n  at: \"?([^\"\n]*)\"?\n", text, re.MULTILINE
    )
    return m.group(2) if m else None


def seed_pages(
    facts: Iterable[Mapping[str, Any]],
    *,
    existing: Mapping[str, str] | None = None,
    today: str | None = None,
) -> dict[str, str]:
    """The ``runspec/`` pages for ``facts`` (seed rows: kind/about/text), keyed by
    repo path, plus ``runspec/index.md``.

    ``existing`` is what the repo holds now: a page whose content is unchanged
    keeps its ``generated.at`` date, so regenerating is a no-op until the seed
    changes."""
    existing = existing or {}
    today = today or _dt.date.today().isoformat()
    by_page: dict[str, list[dict[str, Any]]] = {}
    mentions: dict[str, set[str]] = {}
    order: list[str] = []
    for f in facts:
        about = list(f["about"])
        home = _primary(about)
        for ref in [home, *about]:
            if ref not in by_page:
                by_page[ref] = []
                mentions[ref] = set()
                order.append(ref)
        by_page[home].append(dict(f))
        for ref in about:
            for other in about:
                if other != ref:
                    mentions[ref].add(other)

    pages: dict[str, str] = {}
    for ref in order:
        related = sorted(mentions[ref], key=lambda r: (page_path(r), r))
        body = _page_body(ref, by_page[ref], related)
        path = page_path(ref)
        digest = hashlib.sha256(
            (_frontmatter(ref, by_page[ref], "", "") + body).encode("utf-8")
        ).hexdigest()[:12]
        by = f"{PRODUCER}/{digest}"
        old = existing.get(path, "")
        at = today
        if old and f"  by: {by}\n" in old:
            at = _existing_at(old) or today
        pages[path] = _frontmatter(ref, by_page[ref], by, at) + body

    pages[f"{GENERATED_DIR}/index.md"] = _index(order)
    return pages


def _index(refs: list[str]) -> str:
    groups: dict[str, list[str]] = {}
    for ref in refs:
        heading = ENTITY_PAGES.get(_split(ref)[0], ENTITY_PAGES["topic"])[2]
        groups.setdefault(heading, []).append(ref)
    lines = [
        "# runspec and runspec-console",
        "",
        "Generated from the documentation summaries that ship with runspec-console.",
        "Don't edit these pages by hand: a refresh overwrites them. Put team notes in",
        "the other folders and link here.",
        "",
    ]
    for heading in sorted(groups):
        lines += [f"## {heading}", ""]
        for ref in sorted(groups[heading], key=lambda r: _title(r).lower()):
            lines.append(f"- [{_title(ref)}](/{page_path(ref)})")
        lines.append("")
    return "\n".join(lines)


# ── the template ────────────────────────────────────────────────────────────


def template_static() -> dict[str, bytes]:
    """Every file in ``wiki_template/`` except the generated ``runspec/`` pages
    and Python caches, keyed by repo path."""
    out: dict[str, bytes] = {}
    for path in sorted(TEMPLATE_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(TEMPLATE_DIR).as_posix()
        if (
            rel.startswith(GENERATED_DIR + "/")
            or "__pycache__" in rel
            or rel.endswith((".pyc", "__init__.py"))
        ):
            continue
        # LF always: a Windows checkout of the source (autocrlf) mustn't push
        # CRLF files into a new wiki repo.
        out[rel] = path.read_bytes().replace(b"\r\n", b"\n")
    return out


def committed_pages() -> dict[str, str]:
    """The generated pages as committed in ``wiki_template/runspec/``."""
    base = TEMPLATE_DIR / GENERATED_DIR
    if not base.is_dir():
        return {}
    return {
        p.relative_to(TEMPLATE_DIR).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(base.rglob("*.md"))
    }


def template_files(
    facts: Iterable[Mapping[str, Any]] | None = None, *, today: str | None = None
) -> dict[str, bytes]:
    """The whole starter repo: the static files, the generated ``runspec/``
    pages, and a log entry dated ``today``."""
    today = today or _dt.date.today().isoformat()
    files = template_static()
    pages = seed_pages(
        load_seed() if facts is None else facts,
        existing=committed_pages(),
        today=today,
    )
    files.update({p: t.encode("utf-8") for p, t in pages.items()})
    files["log.md"] = (
        f"# Log\n\n## {today}\n\n- Created from the runspec-console wiki template.\n"
    ).encode("utf-8")
    return files


def refresh_changes(
    current: Mapping[str, str],
    facts: Iterable[Mapping[str, Any]] | None = None,
    *,
    today: str | None = None,
) -> dict[str, bytes | None]:
    """What a refresh changes under ``runspec/``: new or changed pages as bytes,
    pages the seed no longer produces as ``None`` (deleted). ``current`` is the
    repo's ``runspec/`` pages keyed by repo path. Empty when nothing changed."""
    pages = seed_pages(
        load_seed() if facts is None else facts, existing=current, today=today
    )
    changes: dict[str, bytes | None] = {
        p: t.encode("utf-8") for p, t in pages.items() if current.get(p) != t
    }
    for path in current:
        if path not in pages and path.startswith(GENERATED_DIR + "/"):
            changes[path] = None
    return changes


def is_wiki_root(index_md: bytes | str | None) -> bool:
    """Whether a repo's root ``index.md`` is already an OKF bundle root."""
    if not index_md:
        return False
    text = (
        index_md.decode("utf-8", "replace") if isinstance(index_md, bytes) else index_md
    )
    fm = lint_module().split_frontmatter(text)[0]
    return bool(fm and lint_module().top_level_keys(fm).get("okf_version"))


# ── lint ────────────────────────────────────────────────────────────────────

_LINT: ModuleType | None = None


def lint_module() -> ModuleType:
    """``wiki_template/tools/okf_lint.py``, loaded once."""
    global _LINT
    if _LINT is None:
        path = TEMPLATE_DIR / "tools" / "okf_lint.py"
        spec = importlib.util.spec_from_file_location("runspec_okf_lint", path)
        if spec is None or spec.loader is None:  # pragma: no cover
            raise ImportError(f"can't load {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod  # dataclasses look the module up by name
        spec.loader.exec_module(mod)
        _LINT = mod
    return _LINT


def lint_files(files: Mapping[str, str]) -> list[dict[str, Any]]:
    """Lint an in-memory bundle; issues as dicts (level/path/message/line)."""
    from dataclasses import asdict

    return [asdict(i) for i in lint_module().lint_files(dict(files))]


def lint_errors(files: Mapping[str, str]) -> list[str]:
    return [
        f"{i['path']}: {i['message']}"
        for i in lint_files(files)
        if i["level"] == "error"
    ]


# ── CLI: keep the committed pages in step with the seed ─────────────────────


def _write(check: bool) -> int:
    current = committed_pages()
    changes = refresh_changes(current)
    if check:
        for path in sorted(changes):
            print(f"stale: {path}")
        if changes:
            print("run: python -m runspec_console.okf --write")
        return 1 if changes else 0
    for path, data in changes.items():
        target = TEMPLATE_DIR / path
        if data is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        print(f"{'removed' if data is None else 'wrote'} {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Regenerate the wiki template's runspec/ pages from the seed."
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    return _write(check=args.check)


if __name__ == "__main__":
    sys.exit(main())

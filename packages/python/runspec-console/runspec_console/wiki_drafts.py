"""Edits to the team wiki's pages, kept on this console until they're proposed.

The team wiki's pages live in the wiki repo (see ``wiki_pages``). The console
edits them by keeping a *draft*: the page's full new text, under
``{knowledge_dir}/drafts/<page path>``. A draft starts from the cached page (or
a new page when there isn't one) and changes when a fact is added to it or the
operator edits it. **Propose** pushes the drafts to the operator's own
``contrib/<lane>`` branch, where the team reviews them as a merge request.

Facts become page edits here: :func:`target_for` picks the page and section a
fact belongs in, and :func:`add_bullet` adds it as a bullet. Only team facts can
(this console's own facts never leave it — ``knowledge.fact_scope``).

Pure and stdlib-only.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import wiki_pages

DRAFTS_DIR = "drafts"

#: OKF ``type`` for a new page, by entity type.
PAGE_TYPES = {
    "runnable": "Runnable",
    "service": "Service",
    "term": "Term",
    "topic": "Topic",
    "team": "Team",
    "runbook": "Playbook",
    "host": "Host",
}
#: Folder for each entity type (the inverse of ``wiki_pages.ENTITY_DIRS``).
DIR_FOR = {v: k for k, v in wiki_pages.ENTITY_DIRS.items()}
#: Which page a fact goes on when it's about several things, best first.
PAGE_PRIORITY = ("runnable", "service", "term", "topic", "team", "runbook")
#: The sections a new runnable page starts with.
RUNNABLE_SECTIONS = ("What it's for", "Works with", "Watch out for")
#: Fact kinds that describe how tools connect.
CONNECTION_KINDS = frozenset({"pairing", "effect"})

_SLUG = re.compile(r"[^a-z0-9._-]+")
_HEADING2 = re.compile(r"^##\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def slug(name: str) -> str:
    return _SLUG.sub("-", str(name).strip().lower()).strip("-.") or "page"


def page_path_for(entity: str) -> str:
    """``runnable:http-check`` → ``runnables/http-check.md``; ``""`` for an
    entity type the wiki has no folder for (a group, a profile, a file …)."""
    kind, _, name = str(entity).partition(":")
    folder = DIR_FOR.get(kind)
    return f"{folder}/{slug(name)}.md" if folder and name else ""


def _yaml(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def new_page(entity: str, description: str = "") -> str:
    """A new OKF page for ``entity``: frontmatter (``status: draft``), a title,
    and for a runnable the three standard sections."""
    kind, _, name = entity.partition(":")
    lines = [
        "---",
        f"type: {PAGE_TYPES.get(kind, 'Topic')}",
        f"title: {_yaml(name)}",
    ]
    if description:
        lines.append(f"description: {_yaml(description)}")
    lines += ["status: draft", "---", "", f"# {name}", ""]
    if kind == "runnable":
        for heading in RUNNABLE_SECTIONS:
            lines += [f"## {heading}", ""]
            if heading == "What it's for" and description:
                lines += [description, ""]
    return "\n".join(lines)


def text_hash(text: str) -> str:
    """A short fingerprint of a page's text (trailing newlines ignored), to tell
    whether a draft is still the version that was proposed."""
    import hashlib

    return hashlib.sha256(text.rstrip("\n").encode("utf-8")).hexdigest()[:16]


def target_for(fact: dict[str, Any]) -> tuple[str, str] | None:
    """``(entity, heading)`` a fact belongs under, or ``None`` when none of its
    subjects has a page type. On a runnable page, how tools connect goes under
    **Works with** and everything else under **Watch out for**; other pages get
    a **Notes** section."""
    about = [str(a) for a in fact.get("about") or []]
    for kind in PAGE_PRIORITY:
        for ref in about:
            if ref.startswith(kind + ":"):
                if kind == "runnable":
                    heading = (
                        "Works with"
                        if fact.get("kind") in CONNECTION_KINDS
                        else "Watch out for"
                    )
                else:
                    heading = "Notes"
                return ref, heading
    return None


def _norm(text: str) -> str:
    return " ".join(wiki_pages.plain(text).lower().split())


def has_bullet(text: str, bullet: str) -> bool:
    """Whether ``text`` already says ``bullet`` (ignoring formatting and case)."""
    want = _norm(bullet)
    return bool(want) and want in _norm(text)


def add_bullet(text: str, heading: str, bullet: str) -> str:
    """``text`` with ``- bullet`` added at the end of its ``## heading`` section
    (after the section's last line of content), or in a new section at the end
    when there's no such heading. Unchanged when the page already says it."""
    bullet = " ".join(str(bullet).split())
    if not bullet or has_bullet(text, bullet):
        return text
    line = f"- {bullet}"
    lines = text.rstrip("\n").split("\n")
    want = heading.strip().lower()
    start = next(
        (
            i
            for i, ln in enumerate(lines)
            if (m := _HEADING2.match(ln)) and m.group(1).strip().lower() == want
        ),
        None,
    )
    if start is None:
        return "\n".join(lines + ["", f"## {heading}", "", line]) + "\n"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("#")),
        len(lines),
    )
    last = start
    for i in range(start + 1, end):
        if lines[i].strip():
            last = i
    if last == start:  # an empty section: leave a blank line under the heading
        new = lines[: start + 1] + ["", line] + lines[start + 1 :]
    else:
        new = lines[: last + 1] + [line] + lines[last + 1 :]
    return "\n".join(new) + "\n"


# -- the draft store ----------------------------------------------------------


def _draft_file(root: Path, path: str) -> Path:
    rel = path.replace("\\", "/").strip("/")
    if not wiki_pages.is_page_path(rel):
        raise ValueError(f"{path!r} isn't a wiki page path")
    return Path(root) / DRAFTS_DIR / rel


def base_text(root: Path, path: str) -> str | None:
    """The page as the team has it (the cached copy from the last Pull), or
    ``None`` for a page that doesn't exist yet."""
    f = Path(root) / wiki_pages.PAGES_DIR / path
    try:
        return f.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None


def read_draft(root: Path, path: str) -> str | None:
    try:
        return _draft_file(root, path).read_text(encoding="utf-8")
    except OSError:
        return None


def current_text(root: Path, path: str, entity: str = "", description: str = "") -> str:
    """What an edit to ``path`` starts from: its draft, else the team's page,
    else a new page."""
    draft = read_draft(root, path)
    if draft is not None:
        return draft
    base = base_text(root, path)
    if base is not None:
        return base
    return new_page(entity or wiki_pages.entity_for(path), description)


def save_draft(root: Path, path: str, text: str) -> bool:
    """Write ``text`` as the draft of ``path``. A text identical to the team's
    page drops the draft instead. Returns whether a draft is now kept."""
    f = _draft_file(root, path)
    base = base_text(root, path)
    if base is not None and text.rstrip("\n") == base.rstrip("\n"):
        discard_draft(root, path)
        return False
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return True


def discard_draft(root: Path, path: str) -> bool:
    f = _draft_file(root, path)
    try:
        f.unlink()
    except OSError:
        return False
    for parent in (f.parent, f.parent.parent):
        try:
            parent.rmdir()  # only when empty
        except OSError:
            break
    return True


def draft_paths(root: Path) -> list[str]:
    d = Path(root) / DRAFTS_DIR
    if not d.is_dir():
        return []
    return sorted(
        p.relative_to(d).as_posix()
        for p in d.rglob("*.md")
        if wiki_pages.is_page_path(p.relative_to(d).as_posix())
    )


def diff(base: str | None, text: str, path: str) -> str:
    """A unified diff of the draft against the team's page (a new page diffs
    against nothing)."""
    a = (base or "").splitlines()
    b = text.splitlines()
    return "\n".join(
        difflib.unified_diff(
            a,
            b,
            fromfile=f"a/{path}" if base is not None else "/dev/null",
            tofile=f"b/{path}",
            lineterm="",
            n=1,
        )
    )


def list_drafts(root: Path) -> list[dict[str, Any]]:
    """Every draft with its diff: ``{path, entity, new, added, removed, diff}``."""
    out: list[dict[str, Any]] = []
    for path in draft_paths(root):
        text = read_draft(root, path) or ""
        base = base_text(root, path)
        d = diff(base, text, path)
        lines = d.split("\n")
        out.append(
            {
                "path": path,
                "entity": wiki_pages.entity_for(path),
                "new": base is None,
                "added": sum(
                    1 for ln in lines if ln.startswith("+") and not ln.startswith("+++")
                ),
                "removed": sum(
                    1 for ln in lines if ln.startswith("-") and not ln.startswith("---")
                ),
                "diff": d,
            }
        )
    return out


def drop_landed(root: Path) -> list[str]:
    """After a Pull: drop drafts the team's pages now match (the merge request
    was merged). Returns the paths dropped."""
    dropped: list[str] = []
    for path in draft_paths(root):
        base = base_text(root, path)
        draft = read_draft(root, path)
        if (
            base is not None
            and draft is not None
            and base.rstrip("\n") == draft.rstrip("\n")
        ):
            discard_draft(root, path)
            dropped.append(path)
    return dropped


# -- merge request links ------------------------------------------------------


def merge_request_url(repo_url: str, source: str, target: str) -> str:
    """A link that opens the "new merge request" form for ``source`` →
    ``target`` (GitLab), or GitHub's compare view; ``""`` when the repo's web
    address can't be worked out."""
    web = wiki_pages.repo_web_url(repo_url)
    if not web or not source or not target:
        return ""
    if "gitlab" in web.lower():
        return (
            f"{web}/-/merge_requests/new"
            f"?merge_request%5Bsource_branch%5D={quote(source, safe='')}"
            f"&merge_request%5Btarget_branch%5D={quote(target, safe='')}"
        )
    return (
        f"{web}/compare/{quote(target, safe='/')}...{quote(source, safe='/')}?expand=1"
    )

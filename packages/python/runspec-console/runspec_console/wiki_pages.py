"""The team wiki's pages: OKF Markdown in the wiki repo, read by the console.

The wiki repo (see ``okf.py`` and ``wiki_template/``) holds one Markdown page
per thing — ``runnables/<name>.md``, ``services/<name>.md``, ``terms/<term>.md``
— each with YAML frontmatter whose ``type`` says what it is. The team writes and
reviews those pages through the repo. Memory Sync's Pull copies them into a
machine-local cache (``{knowledge_dir}/pages/``) and this module parses them, so
the Knowledge tab can show a runnable's page and the agent gets its "Works with"
and "Watch out for" bullets when it looks at that runnable.

Pure and stdlib-only. Frontmatter is split with the lint's own parser
(``okf.lint_module``), so the console reads a page exactly as the repo's CI
checks it.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

from . import okf

#: The cache directory under the knowledge dir.
PAGES_DIR = "pages"

#: Top-level wiki folders the console reads, and the entity type of their pages.
#: ``runspec/`` is left out: those pages are generated from the console's own
#: docs, which the seed layer already carries.
ENTITY_DIRS: dict[str, str] = {
    "runnables": "runnable",
    "services": "service",
    "terms": "term",
    "topics": "topic",
    "teams": "team",
    "runbooks": "runbook",
    "hosts": "host",
}
#: Files in those folders that aren't pages.
_NOT_PAGES = frozenset({"index.md", "log.md", "readme.md"})
#: Headings whose bullets the agent gets as notes, in this order.
NOTE_HEADINGS = ("works with", "watch out for")
#: Most bytes read from one page (a huge page is cut, not dropped).
MAX_PAGE_BYTES = 200_000

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_VERIFIED_BY = re.compile(r"verified:.*?by:\s*['\"]?([^'\"\n,}\]]+)", re.S)
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)")
_CODE = re.compile(r"`([^`]+)`")
_STRONG = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")


def entity_for(path: str) -> str:
    """``runnables/cron.md`` → ``runnable:cron``; ``""`` for a path that isn't a
    page the console reads."""
    parts = path.replace("\\", "/").strip("/").split("/")
    if len(parts) != 2 or parts[0] not in ENTITY_DIRS:
        return ""
    name = parts[1]
    if not name.lower().endswith(".md") or name.lower() in _NOT_PAGES:
        return ""
    return f"{ENTITY_DIRS[parts[0]]}:{name[:-3]}"


def is_page_path(path: str) -> bool:
    return bool(entity_for(path))


def plain(text: str) -> str:
    """Markdown inline formatting removed: links keep their text, code and bold
    keep their content. For notes and search, not for display."""
    text = _MD_LINK.sub(r"\1", text)
    text = _CODE.sub(r"\1", text)
    text = _STRONG.sub(lambda m: m.group(1) or m.group(2) or "", text)
    return " ".join(text.split())


def _sections(body: str) -> tuple[str, list[dict[str, Any]]]:
    """``(title from the first # heading, [{heading, text, bullets}])``. Text
    before the first ``##`` heading becomes a section with an empty heading."""
    title = ""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] = {"heading": "", "lines": []}
    for line in body.split("\n"):
        m = _HEADING.match(line)
        if (
            m
            and len(m.group(1)) == 1
            and not title
            and not out
            and not any(ln.strip() for ln in cur["lines"])
        ):
            title = m.group(2).strip()
            continue
        if m and len(m.group(1)) == 2:
            out.append(cur)
            cur = {"heading": m.group(2).strip(), "lines": []}
            continue
        cur["lines"].append(line)
    out.append(cur)
    sections: list[dict[str, Any]] = []
    for s in out:
        text = "\n".join(s["lines"]).strip("\n")
        if not s["heading"] and not text.strip():
            continue
        bullets: list[str] = []
        for line in text.split("\n"):
            b = _BULLET.match(line)
            if b:
                bullets.append(b.group(1).strip())
            elif bullets and line.startswith(("  ", "\t")) and line.strip():
                bullets[-1] += " " + line.strip()  # a wrapped bullet
        sections.append({"heading": s["heading"], "text": text, "bullets": bullets})
    return title, sections


def parse_page(path: str, text: str) -> dict[str, Any]:
    """One wiki page, parsed. ``error`` is set (and the rest best-effort) when
    the page has no frontmatter or no ``type``."""
    lint = okf.lint_module()
    fm, body, _line = lint.split_frontmatter(text)
    keys = lint.top_level_keys(fm) if fm is not None else {}
    title, sections = _sections(body)
    verified = _VERIFIED_BY.search(fm or "")
    page: dict[str, Any] = {
        "path": path,
        "entity": entity_for(path),
        "type": keys.get("type", ""),
        "title": keys.get("title") or title or Path(path).stem,
        "description": keys.get("description", ""),
        "status": keys.get("status", ""),
        "stale_after": keys.get("stale_after", ""),
        "verified_by": verified.group(1).strip() if verified else "",
        "sections": sections,
    }
    if fm is None:
        page["error"] = "no frontmatter"
    elif not page["type"]:
        page["error"] = "no type in the frontmatter"
    return page


def section_bullets(page: dict[str, Any], heading: str) -> list[str]:
    """The bullets under ``## <heading>`` (case-insensitive), as written."""
    want = heading.strip().lower()
    for s in page.get("sections") or []:
        if str(s.get("heading", "")).strip().lower() == want:
            return list(s.get("bullets") or [])
    return []


def notes(page: dict[str, Any]) -> list[str]:
    """What the agent gets from a page: its "Works with" then "Watch out for"
    bullets, as plain text."""
    out: list[str] = []
    for h in NOTE_HEADINGS:
        out += [plain(b) for b in section_bullets(page, h) if plain(b)]
    return out


def search_text(page: dict[str, Any]) -> str:
    parts = [page.get("title", ""), page.get("description", "")]
    for s in page.get("sections") or []:
        parts += [s.get("heading", ""), plain(s.get("text", ""))]
    return " ".join(p for p in parts if p)


# -- the local cache ---------------------------------------------------------


def write_cache(root: Path, files: dict[str, bytes]) -> dict[str, int]:
    """Replace ``root/pages`` with the page files from a pull (other paths are
    ignored). Returns ``{pages, added, removed}``."""
    cache = Path(root) / PAGES_DIR
    before = {p for p in _cached_paths(cache)}
    keep = {
        p.replace("\\", "/").strip("/"): data
        for p, data in files.items()
        if is_page_path(p)
    }
    tmp = Path(root) / (PAGES_DIR + ".new")
    if tmp.exists():
        shutil.rmtree(tmp)
    for rel, data in keep.items():
        dest = tmp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data[:MAX_PAGE_BYTES])
    if cache.exists():
        shutil.rmtree(cache)
    if keep:
        tmp.rename(cache)
    elif tmp.exists():
        shutil.rmtree(tmp)
    after = set(keep)
    return {
        "pages": len(after),
        "added": len(after - before),
        "removed": len(before - after),
    }


def _cached_paths(cache: Path) -> list[str]:
    if not cache.is_dir():
        return []
    return sorted(
        p.relative_to(cache).as_posix()
        for p in cache.rglob("*.md")
        if is_page_path(p.relative_to(cache).as_posix())
    )


def read_cache(root: Path) -> list[dict[str, Any]]:
    """Every cached page, parsed, sorted by path. An unreadable file comes back
    as a page with ``error`` set, so one bad page never hides the rest."""
    cache = Path(root) / PAGES_DIR
    out: list[dict[str, Any]] = []
    for rel in _cached_paths(cache):
        try:
            text = (
                (cache / rel).read_bytes()[:MAX_PAGE_BYTES].decode("utf-8", "replace")
            )
            out.append(parse_page(rel, text))
        except OSError as exc:
            out.append(
                {
                    "path": rel,
                    "entity": entity_for(rel),
                    "sections": [],
                    "title": Path(rel).stem,
                    "error": f"unreadable: {exc}",
                }
            )
    return out


# -- links and search ---------------------------------------------------------

_SSH_URL = re.compile(r"^(?:ssh://)?(?:[^@/]+@)?([^:/]+)[:/](.+?)(?:\.git)?/?$")


def repo_web_url(url: str) -> str:
    """The browsable https URL of a git remote, or ``""`` when it can't be
    worked out (a local path, an odd scheme). Credentials are dropped."""
    url = str(url or "").strip()
    if url.startswith(("https://", "http://")):
        scheme, rest = url.split("://", 1)
        rest = rest.split("@", 1)[-1]  # drop user:token@
        rest = rest[:-4] if rest.endswith(".git") else rest
        return f"{scheme}://{rest.rstrip('/')}"
    if url.startswith("ssh://"):
        parts = urlsplit(url)
        path = parts.path.strip("/")
        path = path[:-4] if path.endswith(".git") else path
        return f"https://{parts.hostname}/{path}" if parts.hostname and path else ""
    m = _SSH_URL.match(url)
    if m and not url.startswith(("/", "file:")) and ":" in url:
        return f"https://{m.group(1)}/{m.group(2)}"
    return ""


def repo_file_url(url: str, ref: str, path: str) -> str:
    """A link to ``path`` at ``ref`` on the repo's web UI (GitLab's
    ``/-/blob/``, otherwise GitHub-style ``/blob/``); ``""`` if unknown."""
    base = repo_web_url(url)
    if not base or not ref or not path:
        return ""
    sep = "/-/blob/" if "gitlab" in base.lower() else "/blob/"
    return f"{base}{sep}{ref}/{path}"


def search(
    pages: list[dict[str, Any]], query: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Pages whose text has every word of ``query`` (case-insensitive), best
    first: a title hit, then the most matching words. Each hit is
    ``{entity, path, title, snippet}``."""
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9._-]*", query.lower()) if w]
    if not words:
        return []
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for page in pages:
        text = search_text(page).lower()
        if not all(w in text for w in words):
            continue
        title = str(page.get("title", "")).lower()
        score = sum(text.count(w) for w in words) + (
            10 if any(w in title for w in words) else 0
        )
        idx = min((text.find(w) for w in words), default=0)
        snippet = search_text(page)[max(0, idx - 60) : idx + 140].strip()
        scored.append(
            (
                score,
                -len(scored),
                {
                    "entity": page.get("entity", ""),
                    "path": page.get("path", ""),
                    "title": page.get("title", ""),
                    "snippet": snippet,
                },
            )
        )
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [hit for _s, _i, hit in scored[:limit]]

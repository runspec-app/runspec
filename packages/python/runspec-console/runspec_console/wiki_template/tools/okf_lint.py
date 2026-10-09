#!/usr/bin/env python3
"""okf_lint.py: check a knowledge bundle against Open Knowledge Format v0.2.

Stdlib only, so it runs in any CI image with Python 3.9+ and nothing installed.
runspec-console runs the same file before it pushes pages, so a page that passes
here passes there.

Errors (exit 1):
  - a page (any .md except index.md / log.md) without YAML frontmatter, or
    without a non-empty ``type``
  - index.md / log.md with frontmatter (only the bundle root index.md may carry
    ``okf_version``)
  - secret-looking text (private keys, tokens, ``password = ...``)

Warnings (exit 0 unless --strict):
  - a link to a .md page that isn't in the bundle (OKF tolerates these, so they
    are only warnings)
  - a page whose ``stale_after`` date has passed
  - a root index.md without ``okf_version``

Usage:
  python tools/okf_lint.py [BUNDLE_DIR] [--strict] [--json]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from dataclasses import asdict, dataclass

RESERVED = ("index.md", "log.md")
#: Folders that are never part of the bundle.
SKIP_DIRS = {
    ".git",
    ".github",
    ".gitlab",
    "node_modules",
    "public",
    "site",
    "_site_src",
}
#: Root files that describe the repo rather than the knowledge in it.
SKIP_ROOT_FILES = {"README.md", "CONTRIBUTING.md", "CHANGELOG.md", "LICENSE.md"}

SECRET_PATTERNS = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer token", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._~+/=-]{20,}")),
    (
        "password",
        re.compile(
            r"\b(?:password|passwd|pwd|secret|api[_-]?key|token)\b\s*[:=]\s*"
            r"(?![<{$])[^\s'\"`]{6,}",
            re.IGNORECASE,
        ),
    ),
)

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")


@dataclass
class Issue:
    level: str  # "error" | "warning"
    path: str
    message: str
    line: int = 0


def split_frontmatter(text: str) -> tuple[str | None, str, int]:
    """``(frontmatter or None, body, body's first line number)``."""
    if text.startswith("﻿"):
        text = text[1:]
    if not text.startswith("---"):
        return None, text, 1
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return None, text, 1
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :]), i + 2
    return None, text, 1


def top_level_keys(frontmatter: str) -> dict[str, str]:
    """The unindented ``key: value`` lines of a YAML mapping, values unquoted.

    Enough to check ``type`` / ``okf_version`` / ``stale_after`` without a YAML
    library; nested values come back as ``""``."""
    out: dict[str, str] = {}
    for raw in frontmatter.split("\n"):
        if not raw or raw[0] in " \t#-":
            continue
        m = _KEY.match(raw.rstrip())
        if not m:
            continue
        value = m.group(2).strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[m.group(1)] = value
    return out


def bundle_files(root: str) -> list[str]:
    """Every .md in the bundle, as a ``/``-separated path relative to ``root``."""
    found: list[str] = []
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(
            d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
        )
        rel_dir = os.path.relpath(here, root).replace(os.sep, "/")
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if rel in SKIP_ROOT_FILES:
                continue
            found.append(rel)
    return found


def lint_text(
    rel: str, text: str, pages: set[str], today: _dt.date | None = None
) -> list[Issue]:
    """Check one file. ``pages`` is every .md path in the bundle (for links)."""
    today = today or _dt.date.today()
    issues: list[Issue] = []
    name = rel.rsplit("/", 1)[-1]
    fm, body, body_line = split_frontmatter(text)
    keys = top_level_keys(fm) if fm is not None else {}

    if name in RESERVED:
        if rel == "index.md":
            if fm is not None and set(keys) - {"okf_version"}:
                issues.append(
                    Issue("error", rel, "the root index.md may only carry okf_version")
                )
            if not keys.get("okf_version"):
                issues.append(
                    Issue("warning", rel, 'add okf_version: "0.2" to the root index.md')
                )
        elif fm is not None:
            issues.append(Issue("error", rel, f"{name} must not have frontmatter"))
    else:
        if fm is None:
            issues.append(Issue("error", rel, "no YAML frontmatter (--- ... ---)"))
        elif not keys.get("type"):
            issues.append(Issue("error", rel, "frontmatter has no type"))
        stale = keys.get("stale_after", "")
        if stale:
            try:
                when = _dt.date.fromisoformat(stale[:10])
            except ValueError:
                issues.append(
                    Issue("warning", rel, f"stale_after {stale!r} isn't a date")
                )
            else:
                if when < today:
                    issues.append(
                        Issue("warning", rel, f"stale since {when.isoformat()}")
                    )

    for lineno, line in enumerate(text.split("\n"), start=1):
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                issues.append(
                    Issue(
                        "error", rel, f"looks like a {label} — keep secrets out", lineno
                    )
                )
                break

    base = rel.rsplit("/", 1)[0] if "/" in rel else ""
    for offset, line in enumerate(body.split("\n")):
        for target in _LINK.findall(line):
            path = target.split("#", 1)[0]
            if not path.endswith(".md") or "://" in path or path.startswith("mailto:"):
                continue
            if path.startswith("/"):
                resolved = path.lstrip("/")
            else:
                resolved = os.path.normpath(os.path.join(base, path)).replace(
                    os.sep, "/"
                )
            if resolved not in pages:
                issues.append(
                    Issue(
                        "warning",
                        rel,
                        f"link to missing page {target}",
                        body_line + offset,
                    )
                )
    return issues


def lint_bundle(root: str, today: _dt.date | None = None) -> list[Issue]:
    files = bundle_files(root)
    pages = set(files)
    issues: list[Issue] = []
    if "index.md" not in pages:
        issues.append(Issue("warning", "index.md", "the bundle has no root index.md"))
    for rel in files:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
            issues.extend(lint_text(rel, fh.read(), pages, today))
    return issues


def lint_files(files: dict[str, str], today: _dt.date | None = None) -> list[Issue]:
    """Lint an in-memory bundle (``{path: text}``) — what runspec-console runs
    before a push."""
    pages = {p for p in files if p.endswith(".md") and p not in SKIP_ROOT_FILES}
    issues: list[Issue] = []
    for rel in sorted(pages):
        issues.extend(lint_text(rel, files[rel], pages, today))
    return issues


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check an OKF v0.2 knowledge bundle.")
    ap.add_argument(
        "bundle", nargs="?", default=".", help="bundle directory (default .)"
    )
    ap.add_argument("--strict", action="store_true", help="fail on warnings too")
    ap.add_argument("--json", action="store_true", help="print issues as JSON")
    args = ap.parse_args(argv)
    issues = lint_bundle(args.bundle)
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    if args.json:
        print(json.dumps([asdict(i) for i in issues], indent=2))
    else:
        for i in issues:
            where = f"{i.path}:{i.line}" if i.line else i.path
            print(f"{i.level}: {where}: {i.message}")
        pages = len(bundle_files(args.bundle))
        print(f"{pages} page(s), {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())

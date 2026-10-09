#!/usr/bin/env python3
"""build_site.py: publish the wiki as a static site with MkDocs.

Copies the bundle's pages into a build folder, turns OKF's root-absolute links
(``/runnables/cron.md``) into the relative links MkDocs expects, writes a
``mkdocs.yml`` and runs ``mkdocs build``. The page frontmatter stays: MkDocs
reads it as page metadata (``title`` becomes the page title).

Needs ``pip install mkdocs-material`` (or plain ``mkdocs``, which it falls back
to). CI runs it on release tags; run it locally to preview:

  python tools/build_site.py --serve

Usage:
  python tools/build_site.py [--bundle .] [--out public] [--name "Team wiki"]
                             [--prepare-only] [--serve]
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import shutil
import subprocess
import sys

SKIP_DIRS = {
    ".git",
    ".github",
    ".gitlab",
    "node_modules",
    "public",
    "site",
    "_site_src",
    "tools",
}
SKIP_ROOT_FILES = {"README.md", "CONTRIBUTING.md", "CHANGELOG.md", "LICENSE.md"}
_LINK = re.compile(r"(\[[^\]]*\]\()(/[^)\s#]+\.md)(#[^)\s]*)?(\))")


def relative_links(text: str, rel: str) -> str:
    """Rewrite ``](/a/b.md)`` to a link relative to the page at ``rel``."""
    here = posixpath.dirname(rel) or "."

    def fix(m: re.Match[str]) -> str:
        target = posixpath.relpath(m.group(2).lstrip("/"), here)
        return f"{m.group(1)}{target}{m.group(3) or ''}{m.group(4)}"

    return _LINK.sub(fix, text)


def site_name(bundle: str, explicit: str) -> str:
    if explicit:
        return explicit
    for env in (
        "WIKI_NAME",
        "CI_PROJECT_TITLE",
        "CI_PROJECT_NAME",
        "GITHUB_REPOSITORY",
    ):
        value = os.environ.get(env, "").strip()
        if value:
            return value.rsplit("/", 1)[-1]
    return os.path.basename(os.path.abspath(bundle)) or "Wiki"


def prepare(bundle: str, src: str) -> int:
    """Copy every page into ``src`` with links rewritten; returns the page count."""
    if os.path.isdir(src):
        shutil.rmtree(src)
    count = 0
    for here, dirs, files in os.walk(bundle):
        dirs[:] = sorted(
            d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
        )
        rel_dir = os.path.relpath(here, bundle).replace(os.sep, "/")
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if rel in SKIP_ROOT_FILES:
                continue
            with open(os.path.join(here, name), encoding="utf-8") as fh:
                text = fh.read()
            out = os.path.join(src, *rel.split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(relative_links(text, rel))
            count += 1
    return count


def mkdocs_yml(name: str, src: str, out: str, theme: str) -> str:
    q = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
    lines = [
        f"site_name: {q(name)}",
        f"docs_dir: {q(src)}",
        f"site_dir: {q(out)}",
        "use_directory_urls: true",
        "theme:",
        f"  name: {theme}",
    ]
    if theme == "material":
        lines += ["  features:", "    - search.suggest", "    - navigation.indexes"]
    lines += ["markdown_extensions:", "  - tables", "  - toc:", "      permalink: true"]
    return "\n".join(lines) + "\n"


def has_material() -> bool:
    try:
        import material  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the wiki as a static site.")
    ap.add_argument("--bundle", default=".", help="bundle directory (default .)")
    ap.add_argument(
        "--out", default="public", help="site output folder (default public)"
    )
    ap.add_argument("--name", default="", help="site name (default: the repo name)")
    ap.add_argument(
        "--work", default="_site_src", help="build folder (default _site_src)"
    )
    ap.add_argument(
        "--prepare-only",
        action="store_true",
        help="write the build folder and mkdocs.yml, don't build",
    )
    ap.add_argument("--serve", action="store_true", help="preview with mkdocs serve")
    args = ap.parse_args(argv)

    src = os.path.abspath(os.path.join(args.work, "docs"))
    count = prepare(args.bundle, src)
    config = os.path.join(args.work, "mkdocs.yml")
    theme = "material" if has_material() else "mkdocs"
    with open(config, "w", encoding="utf-8") as fh:
        fh.write(
            mkdocs_yml(
                site_name(args.bundle, args.name), src, os.path.abspath(args.out), theme
            )
        )
    print(f"prepared {count} page(s) in {src} (theme {theme})")
    if args.prepare_only:
        return 0
    verb = ["serve"] if args.serve else ["build", "--clean"]
    return subprocess.call([sys.executable, "-m", "mkdocs", *verb, "-f", config])


if __name__ == "__main__":
    sys.exit(main())

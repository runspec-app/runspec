"""MkDocs hook: publish raw downloadable assets alongside the rendered docs.

Copies a few source-of-truth files from the repo into the built site verbatim,
so they are served as plain downloads (not rendered Markdown/HTML):

  * ``schema/runspec.schema.json`` → ``https://runspec.app/runspec.schema.json``
    — the URL used by the ``#:schema`` line in generated ``runspec.toml`` files
    and by editor TOML validators (Even Better TOML / Taplo).
  * ``.claude/skills/add-runnable/SKILL.md`` →
    ``https://runspec.app/skills/add-runnable.md`` — the raw add-runnable skill,
    so an agent on a machine without access to the (private) source repo can
    fetch it directly (``curl -O https://runspec.app/skills/add-runnable.md``)
    and drop it in as a Claude Code skill. The rendered companion guide lives at
    ``/authoring/``; this is the verbatim skill file. Published under ``skills/``
    so further skills can be added alongside it.
  * ``.claude/skills/mcp-to-runnable/SKILL.md`` →
    ``https://runspec.app/skills/mcp-to-runnable.md`` — the raw mcp-to-runnable
    skill (turn an MCP server into a runspec module + runnable), fetched the same
    way; its rendered companion guide lives at ``/mcp-to-runnable/``.

Single source of truth: each file lives once in the repo; this hook only copies
it into the build output, so the served copy can never drift from the canonical
one. Works for both ``mkdocs serve`` (local) and ``mkdocs build`` (CI).
"""

from __future__ import annotations

import shutil
from pathlib import Path

# (source path relative to repo root, path served under the site root)
ASSETS = [
    (Path("schema") / "runspec.schema.json", Path("runspec.schema.json")),
    (Path(".claude") / "skills" / "add-runnable" / "SKILL.md", Path("skills") / "add-runnable.md"),
    (Path(".claude") / "skills" / "mcp-to-runnable" / "SKILL.md", Path("skills") / "mcp-to-runnable.md"),
]


def on_post_build(config, **kwargs) -> None:
    root = Path(config["config_file_path"]).parent
    site = Path(config["site_dir"])
    for src_relpath, published_relpath in ASSETS:
        dest = site / published_relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / src_relpath, dest)

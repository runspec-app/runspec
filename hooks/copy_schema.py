"""MkDocs hook: publish the canonical JSON Schema alongside the docs.

Copies the repo-root ``schema/runspec.schema.json`` into the built site so it is
served at ``https://runspec.app/runspec.schema.json`` — the URL used by the
``#:schema`` line in generated ``runspec.toml`` files and by editor TOML
validators (Even Better TOML / Taplo).

Single source of truth: the schema lives at ``schema/runspec.schema.json`` (the
same file ``scripts/check_docs.py`` validates against); this hook only copies it
into the build output, so the served copy can never drift from the canonical
one. Works for both ``mkdocs serve`` (local) and ``mkdocs build`` (CI).
"""

from __future__ import annotations

import shutil
from pathlib import Path

SCHEMA_RELPATH = Path("schema") / "runspec.schema.json"
PUBLISHED_NAME = "runspec.schema.json"


def on_post_build(config, **kwargs) -> None:
    root = Path(config["config_file_path"]).parent
    src = root / SCHEMA_RELPATH
    dest = Path(config["site_dir"]) / PUBLISHED_NAME
    shutil.copyfile(src, dest)

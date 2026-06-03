#!/usr/bin/env python3
"""
check_docs.py — guard the docs against drifting from the source of truth.

Three independent checks, each reported separately; the script exits non-zero
if any fails:

  1. version-sync       — every package manifest version has a matching
                          CHANGELOG entry, so releases are always documented.
  2. toml-examples      — every runspec ```toml block in docs/ validates
                          against schema/runspec.schema.json.
  3. format-reference   — the argument-field and type tables in docs/format.md
                          match the schema, so the reference can't silently rot.

Run from the repo root:  python scripts/check_docs.py
Dependencies: jsonschema (tomllib is stdlib on 3.11+).
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
SCHEMA_PATH = REPO / "schema" / "runspec.schema.json"
CHANGELOG = REPO / "CHANGELOG.md"

# Package manifest → CHANGELOG heading prefix. The Node pack is tagged
# `node-<version>` in the changelog; Python uses the bare version.
MANIFESTS = [
    ("packages/python/runspec/pyproject.toml", "python", ""),
    ("packages/node/package.json", "node", "node-"),
]

# Custom (register_type) type names demonstrated in the docs. The JSON Schema's
# `type` enum only knows built-ins, so a `type` value here is allowed through —
# but anything else outside the enum (e.g. a typo) still fails validation.
KNOWN_CUSTOM_TYPES = {"json-file", "port"}

TOML_FENCE = re.compile(r"```toml\n(.*?)```", re.S)

# Doc pages whose ```toml blocks are NOT runspec.toml configs and must not be
# validated against the schema. runspec-chat documents its own
# `jump_hosts.toml` connection file, a different format.
SKIP_FILES = {"runspec-chat.md"}


def _manifest_version(path: Path) -> str:
    if path.suffix == ".toml":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    return str(json.loads(path.read_text(encoding="utf-8"))["version"])


def check_versions() -> list[str]:
    """Every manifest version must have a `## [<prefix><version>]` changelog entry."""
    errors: list[str] = []
    changelog = CHANGELOG.read_text(encoding="utf-8")
    for rel, label, prefix in MANIFESTS:
        version = _manifest_version(REPO / rel)
        heading = f"## [{prefix}{version}]"
        if heading not in changelog:
            errors.append(
                f"{rel}: {label} version {version} has no CHANGELOG entry "
                f"(expected a heading '{heading}')."
            )
    return errors


def _normalize_custom_types(node: object) -> object:
    """Rewrite documented custom `type` values to `str` before validation.

    The schema's `type` enum only knows built-ins, so an example using a
    registered custom type (e.g. `json-file`) would fail. Mapping just those
    known names to a built-in lets the rest of the block validate normally,
    while a *typo* type is left untouched and still fails.
    """
    if isinstance(node, dict):
        result = {k: _normalize_custom_types(v) for k, v in node.items()}
        if result.get("type") in KNOWN_CUSTOM_TYPES:
            result["type"] = "str"
        return result
    if isinstance(node, list):
        return [_normalize_custom_types(item) for item in node]
    return node


def _toml_blocks():
    """Yield (path, line_number, block_text) for every ```toml block in docs/."""
    for md in sorted(DOCS.glob("*.md")):
        if md.name in SKIP_FILES:
            continue
        text = md.read_text(encoding="utf-8")
        for m in TOML_FENCE.finditer(text):
            line = text[: m.start()].count("\n") + 1
            yield md, line, m.group(1)


def check_toml_examples() -> list[str]:
    """Validate every runspec ```toml block against the canonical JSON Schema.

    Skips blocks that (a) aren't valid TOML — placeholder fragments like
    `[<name>]`; or (b) are foreign manifests (`[project]`/`[tool]`/
    `[build-system]`). `type` enum violations for documented custom types are
    tolerated; every other violation (including type typos) is reported.
    """
    try:
        import jsonschema
    except ImportError:
        return ["jsonschema is not installed — run `pip install jsonschema`."]

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    errors: list[str] = []

    for md, line, block in _toml_blocks():
        loc = f"{md.relative_to(REPO)}:{line}"
        try:
            data = tomllib.loads(block)
        except tomllib.TOMLDecodeError:
            continue  # placeholder / illustrative fragment — not a real config
        if not data:
            continue
        if data.keys() & {"project", "tool", "build-system"}:
            continue  # pyproject / foreign manifest snippet
        data = _normalize_custom_types(data)
        block_errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        for err in block_errors:
            where = "/".join(str(p) for p in err.path) or "(root)"
            errors.append(f"{loc}: schema violation at {where}: {err.message}")
    return errors


def _markdown_table_first_column(text: str, heading: str) -> list[str]:
    """Return the first-column cells of the first Markdown table under `heading`."""
    lines = text.splitlines()
    out: list[str] = []
    in_section = False
    in_table = False
    for ln in lines:
        if ln.startswith("#"):
            in_section = heading in ln
            in_table = False
            continue
        if not in_section:
            continue
        stripped = ln.strip()
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            first = cells[0]
            if set(first) <= {"-", ":", " "}:  # the |---|---| separator row
                in_table = True
                continue
            if first.lower() in {"field", "type", "condition"}:  # header row
                continue
            out.append(first)
        elif in_table and not stripped:
            break  # blank line ends the table
    return out


def _backticked(cell: str) -> str | None:
    """Extract the identifier from a leading `code` span in a table cell."""
    m = re.search(r"`([^`]+)`", cell)
    return m.group(1) if m else None


def check_format_reference() -> list[str]:
    """The arg-field and type tables in format.md must match the schema."""
    errors: list[str] = []
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    arg_props = set(schema["$defs"]["arg_def"]["properties"].keys())
    type_enum = set(schema["$defs"]["arg_def"]["properties"]["type"]["enum"])

    format_md = (DOCS / "format.md").read_text(encoding="utf-8")

    # Argument fields table → first column is `field-name`.
    doc_fields = {
        f for cell in _markdown_table_first_column(format_md, "Argument fields")
        if (f := _backticked(cell))
    }
    if doc_fields != arg_props:
        missing = arg_props - doc_fields
        extra = doc_fields - arg_props
        if missing:
            errors.append(
                "format.md 'Argument fields' table is missing schema fields: "
                + ", ".join(sorted(missing))
            )
        if extra:
            errors.append(
                "format.md 'Argument fields' table documents unknown fields: "
                + ", ".join(sorted(extra))
            )

    # Types table → first column is `type-name`.
    doc_types = {
        t for cell in _markdown_table_first_column(format_md, "Types")
        if (t := _backticked(cell))
    }
    if doc_types != type_enum:
        missing = type_enum - doc_types
        extra = doc_types - type_enum
        if missing:
            errors.append(
                "format.md 'Types' table is missing schema types: "
                + ", ".join(sorted(missing))
            )
        if extra:
            errors.append(
                "format.md 'Types' table documents unknown types: "
                + ", ".join(sorted(extra))
            )
    return errors


CHECKS = [
    ("version-sync", check_versions),
    ("toml-examples", check_toml_examples),
    ("format-reference", check_format_reference),
]


def main() -> int:
    failed = False
    for name, fn in CHECKS:
        errors = fn()
        if errors:
            failed = True
            print(f"✗  {name}: {len(errors)} problem(s)")
            for err in errors:
                print(f"     - {err}")
        else:
            print(f"✓  {name}")
    if failed:
        print("\nDocs drift check failed. See problems above.")
        return 1
    print("\nAll docs drift checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

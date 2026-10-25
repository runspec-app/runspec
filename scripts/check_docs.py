#!/usr/bin/env python3
"""
check_docs.py — guard the docs against drifting from the source of truth.

Five independent checks, each reported separately; the script exits non-zero
if any fails:

  1. version-sync       — every package manifest version has a matching
                          CHANGELOG entry, so releases are always documented.
  2. changelog-updated  — a branch that changes a package's shipping source must
                          also update that package's CHANGELOG, so a code change
                          can't ship undocumented (git-diff based; skips cleanly
                          when no base ref is available).
  3. toml-examples      — every runspec ```toml block in docs/ validates
                          against schema/runspec.schema.json.
  4. format-reference   — the argument-field and type tables in docs/format.md
                          match the schema, so the reference can't silently rot.
  5. credential-env     — every credential variable a runnable package (or its
                          -core) names, e.g. GITLAB_TOKEN, is declared as an arg
                          `env` alias in its runspec.toml, so a runspec-console
                          credential reaches the agent without a scope.

Run from the repo root:  python scripts/check_docs.py
Dependencies: jsonschema (tomllib is stdlib on 3.11+).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
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

# ── changelog-updated config ────────────────────────────────────────────────
# Which CHANGELOG a package's source changes are documented in. Most packages
# own a sibling CHANGELOG.md, discovered automatically. The exceptions are wired
# explicitly here so a new, un-wired package trips the check rather than passing
# silently (the exact gap that let `always_show` ship undocumented):
#   ROOT_CHANGELOG_PKGS  — ship under the repo-root CHANGELOG.md (core + node).
#   CHANGELOG_ALIASES    — source dir documented in *another* package's log
#                          (the console frontend ships inside runspec-console).
#   CHANGELOG_IGNORE     — archived / stub packages with no changelog to keep.
ROOT_CHANGELOG_PKGS = {"packages/python/runspec", "packages/node"}
CHANGELOG_ALIASES = {
    "packages/console-ui": "packages/python/runspec-console/CHANGELOG.md",
}
CHANGELOG_IGNORE = {"packages/registry"}  # archived — no changelog maintained

# A changed file counts as shipping source (so it must be logged) when its suffix
# is here and it isn't a test / build artefact (see _CHANGELOG_EXCLUDE_PARTS).
_SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css"}
_CHANGELOG_EXCLUDE_PARTS = {
    "tests", "test", "__tests__", "node_modules", "__pycache__",
    "dist", "build", "coverage", ".venv",
}

# Custom (register_type) type names demonstrated in the docs. The JSON Schema's
# `type` enum only knows built-ins, so a `type` value here is allowed through —
# but anything else outside the enum (e.g. a typo) still fails validation.
KNOWN_CUSTOM_TYPES = {"json-file", "port"}

TOML_FENCE = re.compile(r"```toml\n(.*?)```", re.S)

# A ```toml block is exempt from runspec.toml schema validation when it is a
# *different* TOML format, identified by content (a top-level table only these
# formats use) rather than by filename — so a real runspec.toml example in any
# doc page is always validated, even on pages that also show a config file.
#   project/tool/build-system  → pyproject / foreign manifest snippet
#   hosts                      → runspec-chat `jump_hosts.toml` connection file
#   ssh/refresh                → console `config.toml` ([ssh]/[refresh] tuning)
#   triggers                   → console `runspec_triggers.toml` ([[triggers]])
#   schedule                   → console `runspec_schedules.toml` ([[schedule]])
#   runbook                    → console `runspec_runbooks.toml` ([[runbook]] runbooks)
#   mailboxes                  → console `config.toml` email folder-watch list (legacy)
#   mailbox / watch            → console `config.toml` monitored mailbox + folders list
#   config_sync                → console `config.toml` Config Sync settings
#   config_seed / seed         → console `config.toml` seed / `runspec_seed_state.toml`
#   host                       → console `runspec_hosts.toml` ([[host]] fleet entries)
#   group / profile            → console `runspec_groups.toml` / `runspec_profiles.toml`
#   filter                     → console `runspec_filters.toml` ([[filter]])
#   credential                 → console `runspec_credentials.toml` ([[credential]] metadata)
#   repo                       → console `runspec_repos.toml` ([[repo]] registry)
#   self_service               → console `runspec_self_service.toml`
#   sound                      → console `runspec_sounds.toml` ([[sound]] named-sound registry)
#   telemetry                  → console `config.toml` ([telemetry] OTLP tracing)
#   phoenix                    → console `config.toml` ([phoenix] local trace server)
#   mcp                        → console `config.toml` ([mcp] external MCP servers)
#   local                      → console `config.toml` ([local] venv_globs auto-discovery)
#   gateway                    → runspec-mcp `runspec_mcp.toml` ([gateway] settings)
#   tool_visibility            → console `config.toml` ([tool_visibility] hide lists)
#   marketplace                → console `config.toml` ([marketplace] + [[marketplace.repos]] indexes)
#   improve                    → console `config.toml` ([improve] evaluation feature)
#   eval                       → console eval fixture ([[eval]] tool-selection cases)
#   suite                      → console `runspec_evals.toml` ([[suite]] fixture store)
#   chat_history/workspace/plugins → console `config.toml` sections
#   terminal                   → console `config.toml` ([terminal] PuTTY look)
#   http                       → console `config.toml` ([http] outbound proxy)
# Trade-off: a runspec runnable literally named `ssh`/`refresh`/`hosts`/`triggers`
# would be skipped — none exist in the docs (guarded by check below), and runnable
# names are author-chosen, so this is acceptable.
NON_RUNSPEC_SECTIONS = {
    "project",
    "http",
    "tool",
    "build-system",
    "hosts",
    "host",
    "ssh",
    "refresh",
    "triggers",
    "schedule",
    "runbook",
    "saved_run",
    "rota",
    "mailboxes",
    "mailbox",
    "watch",
    "config_sync",
    "config_seed",
    "seed",
    "console",
    "llm",
    "denylist",
    "group",
    "profile",
    "filter",
    "credential",
    "repo",
    "self_service",
    "sound",
    "chat_history",
    "workspace",
    "marketplace",
    "sandbox",
    "plugins",
    "telemetry",
    "phoenix",
    "mcp",
    "mcp_server",
    "local",
    "tool_visibility",
    "terminal",
    "improve",
    "gateway",
    "eval",  # console eval fixture ([[eval]] tool-selection cases)
    "suite",  # console runspec_evals.toml ([[suite]] synced fixture store)
}


def _manifest_version(path: Path) -> str:
    if path.suffix == ".toml":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    return str(json.loads(path.read_text(encoding="utf-8"))["version"])


def _changelog_headings(path: Path) -> list[str]:
    """The `## ` heading lines of a changelog, or [] if it can't be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.startswith("## ")]


def _version_sync_targets() -> list[tuple[str, str, str, Path]]:
    """Every package to version-check: (manifest rel, label, prefix, changelog).

    Two sources: the explicit ``MANIFESTS`` (core + node ship under the repo-root
    CHANGELOG with their `node-`/bare prefixes), and every other discovered
    package that owns a **sibling** CHANGELOG.md — checked against that changelog
    with a bare version token. A package with no sibling changelog (the core/node
    pair, the aliased `console-ui` frontend build, archived `registry`) isn't
    added here — it's either covered by MANIFESTS or has no release line to sync."""
    targets: list[tuple[str, str, str, Path]] = [
        (rel, label, prefix, CHANGELOG) for rel, label, prefix in MANIFESTS
    ]
    root_manifest_dirs = {(REPO / rel).parent.resolve() for rel, _, _ in MANIFESTS}
    manifests: list[Path] = []
    for pattern in ("packages/*/pyproject.toml", "packages/*/*/pyproject.toml",
                    "packages/*/package.json", "packages/*/*/package.json"):
        manifests.extend(REPO.glob(pattern))
    seen: set[Path] = set()
    for manifest in sorted(manifests):
        pkgdir = manifest.parent
        if "node_modules" in pkgdir.parts or pkgdir.resolve() in root_manifest_dirs:
            continue
        if pkgdir in seen:
            continue
        seen.add(pkgdir)
        sibling = pkgdir / "CHANGELOG.md"
        if not sibling.exists():
            continue  # aliased frontend / archived pkg — nothing to sync here
        rel = pkgdir.relative_to(REPO).as_posix()
        targets.append((manifest.relative_to(REPO).as_posix(), rel, "", sibling))
    return targets


def check_versions() -> list[str]:
    """Every package manifest version must have a matching `## [..]` changelog entry.

    Core + node check the repo-root CHANGELOG with their prefixes; every other
    package checks its own sibling CHANGELOG.md with a bare version token — so a
    version bump that forgot its changelog line fails, for any package, not just
    the reference library."""
    errors: list[str] = []
    heading_cache: dict[Path, list[str]] = {}
    for manifest_rel, label, prefix, changelog in _version_sync_targets():
        version = _manifest_version(REPO / manifest_rel)
        token = f"[{prefix}{version}]"
        key = changelog.resolve()
        headings = heading_cache.setdefault(key, _changelog_headings(changelog))
        # Heading lines look like `## [0.24.0] — date` or, when a release ships
        # both packs together, `## [0.24.0] / [node-0.19.0] — date`; scan for the
        # `[<prefix><ver>]` token in either form.
        if not any(token in ln for ln in headings):
            where = changelog.relative_to(REPO).as_posix()
            errors.append(
                f"{manifest_rel}: {label} version {version} has no {where} entry "
                f"(expected a '## ' heading containing '{token}')."
            )
    return errors


def _git(*args: str) -> str | None:
    """Run a git command at the repo root; return stdout or None on any failure."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout


def _resolve_base() -> str | None:
    """The ref to diff this branch against, or None if none is resolvable.

    Order: an explicit ``DOCS_CHECK_BASE`` (CI passes the PR base sha), then the
    PR base branch from ``GITHUB_BASE_REF``, then the usual default-branch names.
    Returns None when git is unavailable or no candidate exists — the caller then
    skips (a local run on the default branch has nothing to diff)."""
    candidates = [os.environ.get("DOCS_CHECK_BASE")]
    base_ref = os.environ.get("GITHUB_BASE_REF")
    if base_ref:
        candidates += [f"origin/{base_ref}", base_ref]
    candidates += ["origin/main", "origin/master", "main", "master"]
    for cand in candidates:
        if cand and _git("rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}"):
            return cand
    return None


def _branch_changed_files() -> list[str] | None:
    """Files changed on this branch vs its base, or None to skip.

    Diffs the merge-base of the resolved base ref and the working tree, so it
    covers every commit on the branch plus any uncommitted edits — the same set a
    PR contributes — without flagging changes that merely came in from the base."""
    base = _resolve_base()
    if base is None:
        return None
    merge_base = _git("merge-base", base, "HEAD")
    if not merge_base:
        return None
    diff = _git("diff", "--name-only", merge_base.strip())
    if diff is None:
        return None
    return [ln for ln in diff.splitlines() if ln.strip()]


def _changelog_targets() -> tuple[dict[Path, dict], list[str]]:
    """Map each tracked CHANGELOG to the source dir-prefixes that must update it.

    Auto-discovers every package (a dir under ``packages/`` holding a manifest),
    resolving its changelog via the sibling ``CHANGELOG.md``, ``CHANGELOG_ALIASES``,
    or ``ROOT_CHANGELOG_PKGS``. A package with none of these — and not in
    ``CHANGELOG_IGNORE`` — is reported as a wiring gap rather than skipped, so a
    newly-added package can't silently escape the check."""
    targets: dict[Path, dict] = {}
    config_errors: list[str] = []
    manifests: list[Path] = []
    for pattern in ("packages/*/pyproject.toml", "packages/*/*/pyproject.toml",
                    "packages/*/package.json", "packages/*/*/package.json"):
        manifests.extend(REPO.glob(pattern))
    seen: set[str] = set()
    for manifest in sorted(manifests):
        pkgdir = manifest.parent
        if "node_modules" in pkgdir.parts:
            continue
        rel = pkgdir.relative_to(REPO).as_posix()
        if rel in seen or rel in CHANGELOG_IGNORE:
            continue
        seen.add(rel)
        if rel in CHANGELOG_ALIASES:
            changelog = REPO / CHANGELOG_ALIASES[rel]
        elif (pkgdir / "CHANGELOG.md").exists():
            changelog = pkgdir / "CHANGELOG.md"
        elif rel in ROOT_CHANGELOG_PKGS:
            changelog = CHANGELOG
        else:
            config_errors.append(
                f"{rel}: package has no CHANGELOG.md and no wiring in "
                "check_docs.py — add a sibling CHANGELOG.md, or list it in "
                "ROOT_CHANGELOG_PKGS / CHANGELOG_ALIASES / CHANGELOG_IGNORE."
            )
            continue
        entry = targets.setdefault(
            changelog.resolve(), {"prefixes": [], "rel": changelog.relative_to(REPO).as_posix()}
        )
        entry["prefixes"].append(rel + "/")
    return targets, config_errors


def _is_source(path: str) -> bool:
    """True when a changed path is shipping source (so a change must be logged)."""
    parts = path.split("/")
    if any(p in _CHANGELOG_EXCLUDE_PARTS for p in parts):
        return False
    name = parts[-1]
    if ".test." in name or ".spec." in name or name.endswith(".snap"):
        return False
    return Path(name).suffix in _SOURCE_SUFFIXES


def check_changelog_updated() -> list[str]:
    """A branch that changes a package's source must update that package's CHANGELOG.

    version-sync only checks that the *current* manifest version has an entry, so
    a change that ships without bumping the version (or its changelog) slips
    through. This closes that gap: for each tracked package, if the branch touched
    shipping source under it but left its CHANGELOG untouched, that's an error.
    Skips cleanly (no failure) when no base ref is resolvable — e.g. a local run
    on the default branch, where there is no branch diff to inspect."""
    targets, errors = _changelog_targets()
    changed = _branch_changed_files()
    if changed is None:
        print("     (skipped — no base ref to diff against; set DOCS_CHECK_BASE "
              "or fetch origin/main)")
        return errors
    changed_set = set(changed)
    for changelog, info in sorted(targets.items(), key=lambda kv: kv[1]["rel"]):
        prefixes = tuple(info["prefixes"])
        touched_source = sorted(
            f for f in changed
            if f.startswith(prefixes) and _is_source(f)
        )
        if not touched_source:
            continue
        changelog_rel = changelog.relative_to(REPO).as_posix()
        if changelog_rel not in changed_set:
            example = touched_source[0]
            more = f" (+{len(touched_source) - 1} more)" if len(touched_source) > 1 else ""
            errors.append(
                f"{info['rel']}: source changed (e.g. {example}{more}) but "
                f"{changelog_rel} was not updated on this branch — add a "
                "CHANGELOG entry (and bump the version) for the change."
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
        text = md.read_text(encoding="utf-8")
        for m in TOML_FENCE.finditer(text):
            line = text[: m.start()].count("\n") + 1
            yield md, line, m.group(1)


def check_toml_examples() -> list[str]:
    """Validate every runspec ```toml block against the canonical JSON Schema.

    Skips blocks that (a) aren't valid TOML — placeholder fragments like
    `[<name>]`; or (b) are a non-runspec config format, detected by a top-level
    table in ``NON_RUNSPEC_SECTIONS`` (foreign manifests, the chat
    `jump_hosts.toml`, the console `config.toml`). `type` enum violations for
    documented custom types are tolerated; every other violation (including
    type typos) is reported.
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
        if data.keys() & NON_RUNSPEC_SECTIONS:
            continue  # non-runspec config format (manifest / chat / console)
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


# ── credential-env ──────────────────────────────────────────────────────────
# runspec-console applies an unscoped credential only to a runnable that
# declares one of its name-derived env vars (console 0.294.0). A package whose
# core reads <PREFIX>_TOKEN but whose runspec.toml declares nothing works when
# the credential is picked in Forms and silently fails for the agent — found in
# grafana/prometheus/jira/confluence (#812) and again in gitlab.
CREDENTIAL_SUFFIXES = ("TOKEN", "PAT", "PASSWORD", "USERNAME", "USER", "API_TOKEN", "PERSONAL_TOKEN", "API_KEY")
# Packages with the same gap, not yet fixed. Remove an entry once its
# runspec.toml declares the variables.
CREDENTIAL_ENV_KNOWN_GAPS = {"runspec-jsm", "runspec-snow", "runspec-rundeck"}


def _declared_envs(node: object, acc: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "env" and isinstance(value, str):
                acc.add(value)
            elif key == "env" and isinstance(value, list):
                acc.update(v for v in value if isinstance(v, str))
            else:
                _declared_envs(value, acc)
    elif isinstance(node, list):
        for item in node:
            _declared_envs(item, acc)


def check_credential_env() -> list[str]:
    """Credential variables a package names must be declared in its runspec.toml."""
    errors: list[str] = []
    python = REPO / "packages" / "python"
    for spec in sorted(python.glob("runspec-*/runspec_*/runspec.toml")):
        pkg = spec.parent.parent
        if pkg.name in CREDENTIAL_ENV_KNOWN_GAPS:
            continue
        prefix = pkg.name.removeprefix("runspec-").upper().replace("-", "_")
        pattern = re.compile(rf"\b{prefix}_(?:{'|'.join(CREDENTIAL_SUFFIXES)})\b")
        sources = [spec, *pkg.glob("runspec_*/**/*.py")]
        core = python / f"{pkg.name}-core"
        sources += list(core.glob("runspec_*/**/*.py"))
        named = {m for src in sources for m in pattern.findall(src.read_text(encoding="utf-8", errors="ignore"))}
        declared: set[str] = set()
        _declared_envs(tomllib.loads(spec.read_text(encoding="utf-8")), declared)
        missing = sorted(named - declared)
        if missing:
            errors.append(
                f"{pkg.name}: {', '.join(missing)} named in the package but not declared as an arg "
                f"`env` alias in {spec.relative_to(REPO)} — a console credential won't reach the agent"
            )
    return errors


CHECKS = [
    ("version-sync", check_versions),
    ("changelog-updated", check_changelog_updated),
    ("toml-examples", check_toml_examples),
    ("format-reference", check_format_reference),
    ("credential-env", check_credential_env),
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

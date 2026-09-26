"""manifest.py — runtime side of generated department verbs (see ``codegen.py``).

A wrapper package ships the manifest written by ``python -m
runspec_jira_core.codegen`` and dispatches its generated ``raise-*`` verbs
through :func:`run_generated_command`. Kept apart from the generator so
importing the package never imports the CLI module.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError
from runspec_jira_core.requesttypes import RequestTypeSig, create_request, create_request_from_signature

MANIFEST_FORMAT = 1
ON_BEHALF_OF = "on-behalf-of"


def load_manifest(source: str | Path | Any) -> dict[str, Any]:
    """Load a manifest from a path or an ``importlib.resources`` traversable."""
    text = source.read_text(encoding="utf-8") if hasattr(source, "read_text") else Path(source).read_text(encoding="utf-8")
    manifest = json.loads(text)
    if manifest.get("format") != MANIFEST_FORMAT:
        raise JiraError(f"Unsupported request-type manifest format {manifest.get('format')!r}")
    return manifest


def manifest_signatures(manifest: Mapping[str, Any]) -> list[RequestTypeSig]:
    """Every signature in a manifest (pass to ``update_request(signatures=...)``)."""
    return [RequestTypeSig.from_dict(d) for d in manifest["commands"].values()]


def manifest_arg_names(manifest: Mapping[str, Any], command: str) -> list[str]:
    """The arg names a generated verb declares (for reading them off a parsed spec)."""
    sig = RequestTypeSig.from_dict(manifest["commands"][command])
    return [f.name for f in sig.fields if f.kind != "attachment"]


def run_generated_command(
    config: JiraConfig,
    manifest: Mapping[str, Any],
    command: str,
    arg_values: Mapping[str, Any],
    *,
    on_behalf_of: str | None = None,
) -> dict[str, Any]:
    """Raise the request behind a generated verb with its parsed arg values.

    Uses the manifest's ids directly (one POST) when ``config`` targets the
    instance the manifest was generated from; otherwise re-resolves the request
    type by project + name on ``config``'s instance, since ids differ between
    instances.
    """
    if command not in manifest["commands"]:
        raise JiraError(f"{command!r} is not a generated request verb")
    sig = RequestTypeSig.from_dict(manifest["commands"][command])
    if config.url.rstrip("/") == str(manifest.get("url") or "").rstrip("/"):
        return create_request_from_signature(config, sig, arg_values, on_behalf_of=on_behalf_of)
    return create_request(config, sig.project_key, sig.name, arg_values, on_behalf_of=on_behalf_of)

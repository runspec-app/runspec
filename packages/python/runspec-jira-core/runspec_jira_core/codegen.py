"""codegen.py — generate typed raise verbs for a department's JSM projects.

A runspec.toml is static, so a generic runnable can't advertise each request
type's real form. This module generates it instead: for a fixed list of
projects, **one verb per request type** whose args *are* the portal fields
(select → ``choice`` with the allowed options, ``required`` copied, dates get a
date picker). ``runspec serve`` then hands the agent the actual form as the
tool's input schema — raising a request is a single call with no discovery.

It writes two artifacts into the wrapper package, both committed:

* a delimited block of ``[<runnable>.commands.*]`` tables spliced into the
  package's existing ``runspec.toml`` (everything outside the markers is left
  alone, so hand-written verbs live alongside the generated ones), and
* a JSON **manifest** of the signatures, read at runtime by
  :func:`runspec_jira_core.manifest.run_generated_command` so a raise needs no lookup calls at all.

Run it from the wrapper package (stdlib CLI, no runspec dependency)::

    python -m runspec_jira_core.codegen \\
        --runnable mydept --project HR --project PAY --project "Facilities" \\
        --toml mydept/runspec.toml --manifest mydept/jsm_request_types.json

Re-run to pick up form changes; ``--check`` exits 1 when either artifact is
stale (for CI). Request type and option ids are instance-specific: when the
runtime ``JiraConfig`` points at a different instance than the manifest was
generated from (e.g. ``--env uat``), ``run_generated_command`` re-resolves
the request type by project + name live instead of trusting the ids.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from runspec_jira_core.config import JiraConfig, config_from_env
from runspec_jira_core.errors import JiraError
from runspec_jira_core.manifest import MANIFEST_FORMAT, ON_BEHALF_OF
from runspec_jira_core.requesttypes import FieldSig, RequestTypeSig, describe_request_types
from runspec_jira_core.resolve import slugify


def _begin(runnable: str) -> str:
    return f"# >>> runspec-jira-core codegen: {runnable} — generated, do not edit (python -m runspec_jira_core.codegen) >>>"


def _end(runnable: str) -> str:
    return f"# <<< runspec-jira-core codegen: {runnable} <<<"


def _q(text: str) -> str:
    """A TOML basic string (JSON string escapes are valid TOML escapes)."""
    return json.dumps(text, ensure_ascii=False)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def verb_names(sigs: Sequence[RequestTypeSig], *, verb_prefix: str = "raise") -> dict[str, RequestTypeSig]:
    """``{verb: signature}`` — ``raise-<project>-<request type slug>``, de-duplicated."""
    out: dict[str, RequestTypeSig] = {}
    for sig in sigs:
        base = slugify(f"{verb_prefix}-{sig.project_key}-{sig.slug}", max_len=64)
        verb, n = base, 2
        while verb in out:
            verb = f"{base}-{n}"
            n += 1
        out[verb] = sig
    return out


def _arg(f: FieldSig, max_options: int) -> str:
    parts: list[str] = []
    desc = f.label + (f" — {_clip(f.description, 160)}" if f.description else "")
    labels = [o.label for o in f.options]
    use_choice = f.kind in ("select", "multiselect") and 0 < len(labels) <= max_options and len(set(labels)) == len(labels)
    if use_choice:
        parts.append('type = "choice"')
        parts.append(f"options = [{', '.join(_q(x) for x in labels)}]")
    elif f.kind == "number":
        parts.append('type = "float"')
    else:
        parts.append('type = "str"')
        if f.kind in ("select", "multiselect") and labels:
            desc += f" (one of: {_clip(', '.join(labels), 300)})"
    if f.kind in ("multiselect", "multiuser", "labels"):
        parts.append("multiple = true")
    if f.kind in ("date", "datetime"):
        parts.append(f'ui = "{f.kind}"')
    if f.kind == "cascade":
        desc += " (format: 'Parent > Child')"
    if f.kind in ("user", "multiuser"):
        desc += " (username)"
    parts.append(f"required = {'true' if f.required else 'false'}")
    parts.append(f"description = {_q(desc)}")
    return f"{f.name} = {{{', '.join(parts)}}}"


def render_commands(
    runnable: str,
    sigs: Sequence[RequestTypeSig],
    *,
    verb_prefix: str = "raise",
    autonomy: str = "confirm",
    max_options: int = 50,
) -> str:
    """Render the generated ``[<runnable>.commands.*]`` block (markers included)."""
    lines = [_begin(runnable), ""]
    for verb, sig in verb_names(sigs, verb_prefix=verb_prefix).items():
        desc = f"Raise a {sig.name!r} request in {sig.project_name} ({sig.project_key})"
        if sig.description:
            desc += f". {_clip(sig.description, 200)}"
        table = f"{runnable}.commands.{verb}"
        lines += [
            f"[{table}]",
            f"description = {_q(desc)}",
            f'autonomy = "{autonomy}"',
            f"meta = {{jsm = {{project = {_q(sig.project_key)}, service_desk_id = {_q(sig.service_desk_id)}, request_type_id = {_q(sig.request_type_id)}}}}}",
            f"[{table}.args]",
        ]
        lines += [_arg(f, max_options) for f in sig.fields if f.kind != "attachment"]
        if sig.can_raise_on_behalf_of:
            lines.append(f'{ON_BEHALF_OF} = {{type = "str", required = false, description = "Raise on behalf of this customer (username)"}}')
        lines.append("")
    lines.append(_end(runnable))
    return "\n".join(lines) + "\n"


def splice(existing: str, block: str, runnable: str) -> str:
    """Replace the generated block in ``existing`` (or append it when absent)."""
    begin, end = _begin(runnable), _end(runnable)
    head, sep, rest = existing.partition(begin)
    if sep:
        _, sep2, tail = rest.partition(end)
        if not sep2:
            raise JiraError(f"Found the codegen start marker for {runnable!r} but not its end marker")
        return head + block.rstrip("\n") + tail
    return existing.rstrip("\n") + "\n\n" + block


def build_manifest(runnable: str, sigs: Sequence[RequestTypeSig], *, url: str, verb_prefix: str = "raise") -> dict[str, Any]:
    """The runtime manifest: ``{format, runnable, url, commands: {verb: signature}}``."""
    return {
        "format": MANIFEST_FORMAT,
        "runnable": runnable,
        "url": url,
        "commands": {verb: sig.to_dict() for verb, sig in verb_names(sigs, verb_prefix=verb_prefix).items()},
    }


# ── generator CLI ────────────────────────────────────────────────────────────


def generate(
    config: JiraConfig,
    *,
    runnable: str,
    projects: Sequence[str],
    toml_path: Path,
    manifest_path: Path,
    request_types: Sequence[str] | None = None,
    verb_prefix: str = "raise",
    autonomy: str = "confirm",
    max_options: int = 50,
    check: bool = False,
) -> dict[str, Any]:
    """Describe ``projects`` and write (or ``check``) the TOML block + manifest."""
    sigs = describe_request_types(config, projects, request_types=request_types)
    block = render_commands(runnable, sigs, verb_prefix=verb_prefix, autonomy=autonomy, max_options=max_options)
    old_toml = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
    new_toml = splice(old_toml, block, runnable)
    new_manifest = json.dumps(build_manifest(runnable, sigs, url=config.url, verb_prefix=verb_prefix), indent=2, ensure_ascii=False) + "\n"
    old_manifest = manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""
    stale = [str(p) for p, old, new in ((toml_path, old_toml, new_toml), (manifest_path, old_manifest, new_manifest)) if old != new]
    if not check:
        for path, text in ((toml_path, new_toml), (manifest_path, new_manifest)):
            if str(path) in stale:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
    return {"verbs": sorted(verb_names(sigs, verb_prefix=verb_prefix)), "stale" if check else "written": stale}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m runspec_jira_core.codegen", description="Generate typed JSM raise verbs for a set of projects.")
    parser.add_argument("--runnable", required=True, help="Runnable name in the target runspec.toml (e.g. mydept)")
    parser.add_argument("--project", action="append", required=True, help="Project key or name (repeat)")
    parser.add_argument("--request-type", action="append", help="Only these request types — id or name glob (repeat)")
    parser.add_argument("--toml", required=True, type=Path, help="runspec.toml to splice the generated verbs into")
    parser.add_argument("--manifest", required=True, type=Path, help="Manifest JSON to write (ship it as package data)")
    parser.add_argument("--verb-prefix", default="raise")
    parser.add_argument("--autonomy", default="confirm", choices=["autonomous", "confirm", "supervised", "manual"])
    parser.add_argument("--max-options", type=int, default=50, help="Selects with more options become free-text args")
    parser.add_argument("--env", help="dev/uat/prod — selects JIRA_<ENV>_* vars")
    parser.add_argument("--check", action="store_true", help="Exit 1 if the artifacts are out of date; write nothing")
    ns = parser.parse_args(argv)
    try:
        result = generate(
            config_from_env(ns.env),
            runnable=ns.runnable,
            projects=ns.project,
            toml_path=ns.toml,
            manifest_path=ns.manifest,
            request_types=ns.request_type,
            verb_prefix=ns.verb_prefix,
            autonomy=ns.autonomy,
            max_options=ns.max_options,
            check=ns.check,
        )
    except JiraError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 1 if ns.check and result["stale"] else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

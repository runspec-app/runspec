"""emit.py — get Rundeck jobs out of the venv's runnables via the runspec CLI.

`sync` composes two stable runspec CLI surfaces rather than reaching into runspec
internals: ``runspec emit --rundeck --stdout`` (the emitter, shipped in runspec
0.52.0) produces the job JSON, and ``runspec local --format json`` supplies each
runnable's ``x-discoverable`` flag so hidden internal helpers can be filtered
out. Both run the ``runspec`` binary from the wrapper's own venv, so they see
exactly the runnables installed alongside it.

The shell-out functions are thin; the ownership/visibility logic is pure and
unit-tested (`owning_runnable`, `filter_visible`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


class EmitError(Exception):
    """Raised when the `runspec` CLI can't be run or returns unusable output."""


def runspec_bin() -> str:
    """The ``runspec`` binary to invoke — the one in this venv, else on PATH."""
    candidate = Path(sys.executable).parent / "runspec"
    return str(candidate) if candidate.exists() else "runspec"


def _run(argv: list[str]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise EmitError(f"Could not run '{argv[0]}' — is runspec installed in this venv?") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise EmitError(f"{' '.join(argv[:3])} failed (exit {proc.returncode}): {detail[:500]}")
    return proc.stdout


def emit_jobs(
    project: str,
    *,
    group: str | None = None,
    node_filter: str | None = None,
    venv: str | None = None,
    runnable: str | None = None,
    bin_path: str | None = None,
) -> list[dict[str, Any]]:
    """Run ``runspec emit --rundeck --stdout`` and return the job objects.

    ``project`` is passed through so the emitted UUIDs match the import target
    (identity is ``uuid5(project/group/job)``). ``group`` / ``node_filter`` /
    ``venv`` / ``runnable`` map to the matching emit flags.
    """
    argv = [bin_path or runspec_bin(), "emit", "--rundeck", "--stdout", "--project", project]
    if group:
        argv += ["--group", group]
    if node_filter:
        argv += ["--node-filter", node_filter]
    if venv:
        argv += ["--venv", venv]
    if runnable:
        argv += ["--runnable", runnable]
    out = _run(argv)
    if not out.strip():
        return []
    try:
        jobs = json.loads(out)
    except json.JSONDecodeError as exc:
        raise EmitError(f"runspec emit did not return valid JSON: {exc}") from exc
    if not isinstance(jobs, list):
        raise EmitError("runspec emit --stdout did not return a JSON array of jobs.")
    return jobs


def discover_visibility(bin_path: str | None = None) -> tuple[set[str], set[str]]:
    """Return ``(all_runnable_names, hidden_runnable_names)`` from ``runspec local``.

    ``runspec local --format json`` yields an array of discovery records
    (``{runnable, spec: {discoverable, …}}``); hidden = ``spec.discoverable`` is
    an explicit ``false``. Best-effort: any failure yields empty sets, so a
    discovery hiccup never blocks a sync (everything stays visible).
    """
    try:
        out = _run([bin_path or runspec_bin(), "local", "--format", "json"])
        data = json.loads(out)
    except (EmitError, json.JSONDecodeError):
        return set(), set()

    names: set[str] = set()
    hidden: set[str] = set()
    if isinstance(data, list):
        for rec in data:
            if not isinstance(rec, dict):
                continue
            name = rec.get("runnable")
            if not name:
                continue
            names.add(name)
            if (rec.get("spec") or {}).get("discoverable") is False:
                hidden.add(name)
    elif isinstance(data, dict):
        # Fallback for a name-keyed schema shape (x-discoverable marker).
        names = set(data.keys())
        hidden = {n for n, tool in data.items() if isinstance(tool, dict) and tool.get("x-discoverable") is False}
    return names, hidden


def owning_runnable(job_name: str, names: set[str]) -> str | None:
    """Which runnable a leaf job belongs to — the longest matching name.

    A job is named ``<runnable>`` (no subcommands) or ``<runnable>_<verb>…``. With
    the full set of runnable names, the owner is the longest ``n`` in ``names``
    where ``job_name == n`` or ``job_name`` starts with ``n + "_"``; the
    underscore boundary keeps ``foobar_x`` from matching runnable ``foo``.
    """
    best: str | None = None
    for name in names:
        if (job_name == name or job_name.startswith(name + "_")) and (best is None or len(name) > len(best)):
            best = name
    return best


def filter_visible(
    jobs: list[dict[str, Any]],
    names: set[str],
    hidden: set[str],
    *,
    include_hidden: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split jobs into (kept, dropped-job-names) by their runnable's visibility.

    A job whose owning runnable is hidden is dropped unless ``include_hidden``.
    Jobs with no resolvable owner (or when discovery returned nothing) are kept —
    filtering is best-effort curation, never a safety gate.
    """
    if include_hidden or not hidden:
        return jobs, []
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for job in jobs:
        owner = owning_runnable(str(job.get("name", "")), names)
        if owner is not None and owner in hidden:
            dropped.append(str(job.get("name", "")))
        else:
            kept.append(job)
    return kept, dropped


def load_job_files(paths: list[str]) -> list[dict[str, Any]]:
    """Read job objects from files/dirs. A dir contributes its ``*.json``.

    Each file is a Rundeck job-json array (or a single job object); all are
    flattened into one list.
    """
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(p.glob("*.json")))
        elif p.exists():
            files.append(p)
        else:
            raise EmitError(f"No such file or directory: {raw}")

    jobs: list[dict[str, Any]] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EmitError(f"Could not read job file {f}: {exc}") from exc
        if isinstance(data, list):
            jobs.extend(data)
        elif isinstance(data, dict):
            jobs.append(data)
        else:
            raise EmitError(f"{f} is not a Rundeck job object or array.")
    return jobs

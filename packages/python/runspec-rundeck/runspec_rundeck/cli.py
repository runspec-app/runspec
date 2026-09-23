"""cli.py — the `rundeck` runnable entry point.

Parses args with runspec, resolves a `RundeckConfig` (env vars + arg overrides),
and dispatches the active verb (`sync` / `push` / `list`) to the
`runspec-rundeck-core` ops. Output is a JSON object on stdout; a job-level
failure exits non-zero so a CI publish stage fails loudly.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs
from runspec_rundeck_core import (
    RundeckConfig,
    RundeckError,
    config_from_env,
    import_error_details,
    list_jobs,
    prune_jobs,
    sync_target,
)
from runspec_rundeck_core.jobs import ImportResult

from runspec_rundeck.emit import EmitError, discover_visibility, emit_jobs, filter_visible, load_job_files


def _emit(payload: Any) -> None:
    print(json.dumps(payload, default=str))


def _resolve_config(spec: rs.RunSpec) -> RundeckConfig:
    """Build the target config from --env plus any explicit arg overrides."""
    api_version = spec.get("api-version").value
    return config_from_env(
        spec.get("env").value,
        url=spec.get("url").value,
        token=spec.token.value,
        project=spec.project.value,
        api_version=int(api_version) if api_version is not None else None,
        ca_bundle=spec.get("ca-bundle").value,
        proxy=spec.proxy.value,
        tls_verify=False if spec.insecure.value else None,
    )


def _sync(spec: rs.RunSpec, config: RundeckConfig) -> dict[str, Any]:
    project = config.require_project()
    jobs = emit_jobs(
        project,
        group=spec.get("group").value,
        node_filter=spec.get("node-filter").value,
        venv=spec.venv.value,
        runnable=spec.runnable.value,
    )
    names, hidden = discover_visibility()
    jobs, dropped = filter_visible(jobs, names, hidden, include_hidden=bool(spec.all.value))

    if not jobs:
        return {"ok": True, "action": "sync", "project": project, "imported": [], "skipped": [], "failed": [], "pruned": [], "hidden_skipped": dropped, "note": "no jobs to sync"}

    result = sync_target(
        config,
        jobs,
        project=project,
        prune=bool(spec.prune.value),
        create_project=bool(spec.get("create-project").value),
        dupe_option=spec.get("dupe-option").value,
        uuid_option=spec.get("uuid-option").value,
    )
    return {
        "ok": result.ok,
        "action": "sync",
        "project": result.project,
        "created_project": result.created_project,
        "imported": [j.get("name") for j in result.imported],
        "skipped": [j.get("name") for j in result.skipped],
        "failed": _failed_details(result.failed),
        "pruned": [p.get("name") for p in result.pruned],
        "hidden_skipped": dropped,
    }


def _push(spec: rs.RunSpec, config: RundeckConfig) -> dict[str, Any]:
    project = config.require_project()
    path = spec.path.value
    if not path:
        raise EmitError("push needs a job file or directory (e.g. `rundeck push jobs/`).")
    jobs = load_job_files([str(path)])

    result = sync_target(
        config,
        jobs,
        project=project,
        prune=bool(spec.prune.value),
        create_project=bool(spec.get("create-project").value),
        dupe_option=spec.get("dupe-option").value,
        uuid_option=spec.get("uuid-option").value,
    )
    # push prunes by the explicit --group (sync derives groups from the jobs).
    group = spec.get("group").value
    if spec.prune.value and result.ok and group:
        keep = {str(j["uuid"]) for j in jobs if j.get("uuid")}
        result.pruned = prune_jobs(config, group, keep, project=project)

    return {
        "ok": result.ok,
        "action": "push",
        "project": result.project,
        "created_project": result.created_project,
        "imported": [j.get("name") for j in result.imported],
        "skipped": [j.get("name") for j in result.skipped],
        "failed": _failed_details(result.failed),
        "pruned": [p.get("name") for p in result.pruned],
    }


def _list(spec: rs.RunSpec, config: RundeckConfig) -> dict[str, Any]:
    project = config.require_project()
    jobs = list_jobs(config, project=project, group=spec.get("group").value)
    return {
        "ok": True,
        "action": "list",
        "project": project,
        "jobs": [{"id": j.get("id"), "name": j.get("name"), "group": j.get("group")} for j in jobs],
    }


def _failed_details(failed: list[dict[str, Any]]) -> list[str]:
    return import_error_details(ImportResult(failed=failed))


def _dispatch(spec: rs.RunSpec, config: RundeckConfig) -> dict[str, Any]:
    command = spec.runspec_command
    if command == "sync":
        return _sync(spec, config)
    if command == "push":
        return _push(spec, config)
    if command == "list":
        return _list(spec, config)
    raise SystemExit("no command — see `rundeck --help`")  # pragma: no cover


def main() -> None:
    spec = rs.parse("rundeck")
    try:
        config = _resolve_config(spec)
        result = _dispatch(spec, config)
    except (RundeckError, EmitError, ValueError) as exc:
        _emit({"error": str(exc), "kind": type(exc).__name__})
        sys.exit(1)
    _emit(result)
    if not result.get("ok", True):
        sys.exit(1)


if __name__ == "__main__":
    main()

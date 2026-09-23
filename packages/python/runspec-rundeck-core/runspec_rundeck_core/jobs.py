"""jobs.py — Rundeck job import / list / delete / prune, and the sync composition.

Idempotency is by **UUID**: `runspec emit --rundeck` bakes a stable
``uuid5(project/group/job)`` into every job, so importing with
``dupeOption=update`` + ``uuidOption=preserve`` **updates** a matching job and
**creates** a missing one — rename-safe, since identity is the UUID, not the
name. `prune_jobs` then deletes jobs in a managed group whose UUID wasn't in the
push, so a removed runnable's job is cleaned up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from runspec_rundeck_core.client import RundeckConfig, rundeck_request
from runspec_rundeck_core.projects import ensure_project


@dataclass
class ImportResult:
    """Outcome of one job-import call, mirroring Rundeck's response arrays."""

    succeeded: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


@dataclass
class SyncResult:
    """Outcome of a full sync — import plus optional project-create and prune."""

    project: str
    imported: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    pruned: list[dict[str, Any]] = field(default_factory=list)
    created_project: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed


def import_jobs(
    config: RundeckConfig,
    jobs: list[dict[str, Any]],
    *,
    project: str | None = None,
    dupe_option: str = "update",
    uuid_option: str = "preserve",
) -> ImportResult:
    """Import (create/update) a list of job objects into a project.

    ``dupe_option`` is ``update`` (default) / ``create`` / ``skip``;
    ``uuid_option`` is ``preserve`` (default, so the emitted UUID drives
    create-vs-update) / ``remove``. The job JSON array is sent as the request
    body under ``Content-Type: application/json``.
    """
    name = config.require_project(project)
    body = json.dumps(jobs)
    data = rundeck_request(
        "POST",
        f"/project/{name}/jobs/import",
        config=config,
        params={"format": "json", "dupeOption": dupe_option, "uuidOption": uuid_option},
        content=body,
        content_type="application/json",
    )
    data = data or {}
    return ImportResult(
        succeeded=list(data.get("succeeded") or []),
        failed=list(data.get("failed") or []),
        skipped=list(data.get("skipped") or []),
    )


def list_jobs(config: RundeckConfig, *, project: str | None = None, group: str | None = None) -> list[dict[str, Any]]:
    """List jobs in a project, optionally scoped to a group path (and its subgroups)."""
    name = config.require_project(project)
    params = {"groupPath": group} if group else None
    data = rundeck_request("GET", f"/project/{name}/jobs", config=config, params=params)
    return list(data or [])


def delete_job(config: RundeckConfig, job_id: str) -> None:
    """Delete one job by its UUID/id."""
    rundeck_request("DELETE", f"/job/{job_id}", config=config)


def prune_jobs(
    config: RundeckConfig,
    group: str,
    keep_uuids: set[str],
    *,
    project: str | None = None,
) -> list[dict[str, Any]]:
    """Delete jobs in ``group`` whose id is not in ``keep_uuids``.

    Returns the deleted jobs (``{id, name, group}``). Scoped to a single managed
    group so it can never touch jobs the sync doesn't own.
    """
    name = config.require_project(project)
    deleted: list[dict[str, Any]] = []
    for job in list_jobs(config, project=name, group=group):
        job_id = job.get("id")
        if job_id and job_id not in keep_uuids:
            delete_job(config, job_id)
            deleted.append({"id": job_id, "name": job.get("name"), "group": job.get("group")})
    return deleted


def sync_target(
    config: RundeckConfig,
    jobs: list[dict[str, Any]],
    *,
    project: str | None = None,
    prune: bool = False,
    create_project: bool = False,
    dupe_option: str = "update",
    uuid_option: str = "preserve",
) -> SyncResult:
    """Import ``jobs`` into a project, optionally creating it first and pruning stale jobs.

    Prune scope is exactly the groups the pushed jobs declare, keeping the UUIDs
    just pushed — so a job removed from the venv (no longer emitted) is deleted,
    but nothing outside the managed groups is touched. Raises `RundeckError` on a
    transport/auth failure; job-level failures are returned in ``.failed`` for the
    caller to surface (and exit non-zero on).
    """
    name = config.require_project(project)
    result = SyncResult(project=name)

    if create_project:
        result.created_project = ensure_project(config, name)

    imported = import_jobs(config, jobs, project=name, dupe_option=dupe_option, uuid_option=uuid_option)
    result.imported = imported.succeeded
    result.skipped = imported.skipped
    result.failed = imported.failed

    if prune and imported.ok:
        keep = {str(job["uuid"]) for job in jobs if job.get("uuid")}
        groups = {str(job.get("group")) for job in jobs if job.get("group")}
        for group in sorted(groups):
            result.pruned.extend(prune_jobs(config, group, keep, project=name))

    return result


def summarize_import(result: ImportResult) -> str:
    """A one-line human summary of an import."""
    parts = [f"{len(result.succeeded)} imported"]
    if result.skipped:
        parts.append(f"{len(result.skipped)} skipped")
    if result.failed:
        parts.append(f"{len(result.failed)} failed")
    return ", ".join(parts)


def import_error_details(result: ImportResult) -> list[str]:
    """Per-job error strings for the failed entries, for a clear CI report."""
    out: list[str] = []
    for job in result.failed:
        label = job.get("name") or job.get("id") or "?"
        error = job.get("error") or job.get("errorMessage") or "unknown error"
        out.append(f"{label}: {error}")
    return out

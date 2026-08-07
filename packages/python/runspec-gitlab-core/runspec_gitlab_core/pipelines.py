"""pipelines.py — CI/CD pipeline + job read operations (GitLab REST v4)."""

from __future__ import annotations

from typing import Any

from runspec_gitlab_core.client import GitlabConfig, gitlab_request
from runspec_gitlab_core.projects import _project


def list_pipelines(
    config: GitlabConfig,
    project: str | None = None,
    ref: str | None = None,
    status: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """List a project's pipelines, optionally filtered by ``ref`` (branch/tag)
    and ``status`` (e.g. ``success``/``failed``/``running``)."""
    return gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/pipelines",
        config=config,
        params={"ref": ref, "status": status, "per_page": per_page, "page": page},
    )


def get_pipeline_jobs(
    config: GitlabConfig,
    pipeline_id: int,
    project: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """List the jobs of a pipeline (by pipeline id)."""
    return gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/pipelines/{pipeline_id}/jobs",
        config=config,
        params={"per_page": per_page, "page": page},
    )


def get_job_log(
    config: GitlabConfig,
    job_id: int,
    project: str | None = None,
    tail: int | None = None,
) -> dict[str, Any]:
    """Fetch a job's log (trace) as plain text.

    ``tail`` returns only the last N lines — logs are large, so an agent should
    usually pass one. Returns ``{job_id, trace, truncated}``.
    """
    text = gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/jobs/{job_id}/trace",
        config=config,
        raw=True,
    )
    truncated = False
    if tail is not None and tail >= 0:
        lines = text.splitlines()
        if len(lines) > tail:
            text = "\n".join(lines[-tail:])
            truncated = True
    return {"job_id": job_id, "trace": text, "truncated": truncated}

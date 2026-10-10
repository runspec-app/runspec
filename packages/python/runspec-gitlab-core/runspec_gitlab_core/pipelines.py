"""pipelines.py — CI/CD pipeline + job operations (GitLab REST v4): reads + writes."""

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


def _variables(variables: list[str] | None) -> list[dict[str, str]] | None:
    """Parse ``KEY=VALUE`` strings into GitLab's pipeline-variable list."""
    if not variables:
        return None
    parsed = []
    for item in variables:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"Bad variable {item!r} — expected KEY=VALUE")
        parsed.append({"key": key.strip(), "value": value, "variable_type": "env_var"})
    return parsed


def create_pipeline(
    config: GitlabConfig,
    ref: str,
    variables: list[str] | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Start a new pipeline on ``ref``; ``variables`` are ``KEY=VALUE`` strings."""
    return gitlab_request(
        "POST",
        f"/projects/{_project(config, project)}/pipeline",
        config=config,
        body={"ref": ref, "variables": _variables(variables)},
    )


def retry_pipeline(config: GitlabConfig, pipeline_id: int, project: str | None = None) -> dict[str, Any]:
    """Retry a pipeline's failed or canceled jobs."""
    return gitlab_request(
        "POST",
        f"/projects/{_project(config, project)}/pipelines/{pipeline_id}/retry",
        config=config,
    )


def cancel_pipeline(config: GitlabConfig, pipeline_id: int, project: str | None = None) -> dict[str, Any]:
    """Cancel a pipeline's running jobs."""
    return gitlab_request(
        "POST",
        f"/projects/{_project(config, project)}/pipelines/{pipeline_id}/cancel",
        config=config,
    )


def retry_job(config: GitlabConfig, job_id: int, project: str | None = None) -> dict[str, Any]:
    """Retry a single job (creates a new job; returns it)."""
    return gitlab_request(
        "POST",
        f"/projects/{_project(config, project)}/jobs/{job_id}/retry",
        config=config,
    )


def cancel_job(config: GitlabConfig, job_id: int, project: str | None = None) -> dict[str, Any]:
    """Cancel a single running job."""
    return gitlab_request(
        "POST",
        f"/projects/{_project(config, project)}/jobs/{job_id}/cancel",
        config=config,
    )


def play_job(
    config: GitlabConfig,
    job_id: int,
    variables: list[str] | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Run a manual job; ``variables`` are ``KEY=VALUE`` strings."""
    parsed = _variables(variables)
    return gitlab_request(
        "POST",
        f"/projects/{_project(config, project)}/jobs/{job_id}/play",
        config=config,
        body={"job_variables_attributes": [{"key": v["key"], "value": v["value"]} for v in parsed]} if parsed else None,
    )


def delete_pipeline(config: GitlabConfig, pipeline_id: int, project: str | None = None) -> dict[str, Any]:
    """Delete a pipeline with its jobs, logs and artifacts. GitLab only allows
    this for project owners."""
    gitlab_request("DELETE", f"/projects/{_project(config, project)}/pipelines/{pipeline_id}", config=config)
    return {"deleted": "pipeline", "pipeline_id": pipeline_id}

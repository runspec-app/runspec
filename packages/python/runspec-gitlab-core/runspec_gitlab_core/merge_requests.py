"""merge_requests.py — merge-request read operations (GitLab REST v4)."""

from __future__ import annotations

from typing import Any

from runspec_gitlab_core.client import GitlabConfig, gitlab_request
from runspec_gitlab_core.projects import _project, resolve_project_path


def list_merge_requests(
    config: GitlabConfig,
    project: str | None = None,
    state: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """List a project's merge requests.

    ``state`` is ``opened``/``closed``/``merged``/``locked``/``all``.
    """
    return gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/merge_requests",
        config=config,
        params={"state": state, "per_page": per_page, "page": page},
    )


def get_merge_request(config: GitlabConfig, mr_iid: int, project: str | None = None) -> dict[str, Any]:
    """Fetch a single merge request by its project-scoped internal id (``iid``)."""
    return gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/merge_requests/{mr_iid}",
        config=config,
    )


def get_merge_request_diffs(
    config: GitlabConfig,
    mr_iid: int,
    project: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """List a merge request's file diffs.

    Mirrors the canonical ``get_merge_request_diffs`` MCP tool — the paginated
    ``/merge_requests/:iid/diffs`` endpoint, not the deprecated ``/changes``.
    """
    return gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/merge_requests/{mr_iid}/diffs",
        config=config,
        params={"per_page": per_page, "page": page},
    )


def get_merge_request_commits(
    config: GitlabConfig,
    mr_iid: int,
    project: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """List the commits on a merge request (canonical ``get_merge_request_commits``)."""
    return gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/merge_requests/{mr_iid}/commits",
        config=config,
        params={"per_page": per_page, "page": page},
    )


def get_merge_request_notes(
    config: GitlabConfig,
    mr_iid: int,
    project: str | None = None,
    sort: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """List a merge request's notes (comments) — the REST equivalent of the
    canonical ``get_merge_request_notes`` tool. ``sort`` is ``asc``/``desc``."""
    return gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/merge_requests/{mr_iid}/notes",
        config=config,
        params={"sort": sort, "per_page": per_page, "page": page},
    )


def get_merge_request_pipelines(
    config: GitlabConfig,
    mr_iid: int,
    project: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """List the pipelines run for a merge request (canonical ``get_merge_request_pipelines``)."""
    return gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/merge_requests/{mr_iid}/pipelines",
        config=config,
        params={"per_page": per_page, "page": page},
    )


def get_merge_request_conflicts(config: GitlabConfig, mr_iid: int, project: str | None = None) -> Any:
    """Fetch a merge request's conflict files (the canonical ``get_merge_request_conflicts``).

    **Best-effort — not a public REST API.** GitLab exposes MR conflict *contents*
    only through its web-UI endpoint (``{base}/{namespace}/{project}/-/merge_requests/
    :iid/conflicts``), not under ``/api/v4``; the MCP server's own tool calls an
    internal service in-process. This relays that endpoint's JSON verbatim (files
    with conflict markers/sections), so its shape is GitLab's, not ours, and may
    change between versions. Requires a token with read access to the MR (and,
    per GitLab, push access to the source branch). A non-conflicting or
    unmergeable MR typically yields a non-2xx surfaced as :class:`GitlabError`.
    """
    path = resolve_project_path(config, project)
    return gitlab_request(
        "GET",
        f"/{path}/-/merge_requests/{mr_iid}/conflicts",
        config=config,
        api=False,
    )

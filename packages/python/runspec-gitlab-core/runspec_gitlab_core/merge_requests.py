"""merge_requests.py — merge-request read operations (GitLab REST v4)."""

from __future__ import annotations

from typing import Any

from runspec_gitlab_core.client import GitlabConfig, gitlab_request
from runspec_gitlab_core.projects import _project


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


def get_merge_request_changes(config: GitlabConfig, mr_iid: int, project: str | None = None) -> dict[str, Any]:
    """Fetch a merge request together with its file changes / diffs."""
    return gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/merge_requests/{mr_iid}/changes",
        config=config,
    )

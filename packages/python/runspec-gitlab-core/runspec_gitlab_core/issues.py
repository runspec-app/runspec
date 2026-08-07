"""issues.py — project issue read operations (GitLab REST v4)."""

from __future__ import annotations

from typing import Any

from runspec_gitlab_core.client import GitlabConfig, gitlab_request
from runspec_gitlab_core.projects import _project


def list_issues(
    config: GitlabConfig,
    project: str | None = None,
    state: str | None = None,
    labels: str | None = None,
    search: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """List a project's issues.

    ``state`` is ``opened``/``closed``/``all``; ``labels`` is a comma-separated
    label list; ``search`` matches title/description.
    """
    return gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/issues",
        config=config,
        params={
            "state": state,
            "labels": labels,
            "search": search,
            "per_page": per_page,
            "page": page,
        },
    )


def get_issue(config: GitlabConfig, issue_iid: int, project: str | None = None) -> dict[str, Any]:
    """Fetch a single issue by its project-scoped internal id (``iid``)."""
    return gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/issues/{issue_iid}",
        config=config,
    )

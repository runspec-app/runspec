"""projects.py — identity + project read operations (GitLab REST v4)."""

from __future__ import annotations

from typing import Any

from runspec_gitlab_core.client import GitlabConfig, GitlabError, encode_project, gitlab_request


def _project(config: GitlabConfig, project: str | None) -> str:
    """Resolve the target project, falling back to the config default, and
    percent-encode it for the URL path."""
    target = project or config.project
    if not target:
        raise GitlabError("No project — pass project or set it on the config (GITLAB_PROJECT)")
    return encode_project(target)


def whoami(config: GitlabConfig) -> dict[str, Any]:
    """Return the authenticated user (``GET /user``) — a quick token/identity check."""
    return gitlab_request("GET", "/user", config=config)


def get_project(config: GitlabConfig, project: str | None = None) -> dict[str, Any]:
    """Fetch a single project's metadata by numeric id or ``namespace/project`` path."""
    return gitlab_request("GET", f"/projects/{_project(config, project)}", config=config)


def search_projects(
    config: GitlabConfig,
    search: str,
    membership: bool = False,
    per_page: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """Search visible projects by name/path.

    ``membership`` limits results to projects the token's user is a member of.
    """
    return gitlab_request(
        "GET",
        "/projects",
        config=config,
        params={
            "search": search,
            "membership": membership,
            "order_by": "last_activity_at",
            "per_page": per_page,
            "page": page,
        },
    )

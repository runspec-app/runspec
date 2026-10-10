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


def resolve_project_path(config: GitlabConfig, project: str | None = None) -> str:
    """Resolve the target project to its ``namespace/path`` form for non-API web
    endpoints (which need real slashes, not a numeric id or a %2F-encoded path).

    A numeric id is resolved to ``path_with_namespace`` via one API call; a path
    is returned as-is.
    """
    target = project or config.project
    if not target:
        raise GitlabError("No project — pass project or set it on the config (GITLAB_PROJECT)")
    target = str(target)
    if target.isdigit():
        path = get_project(config, target).get("path_with_namespace")
        if not path:
            raise GitlabError(f"Could not resolve a path for project id {target}")
        return str(path)
    return target


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


def resolve_user_ids(config: GitlabConfig, usernames: list[str] | None) -> list[int] | None:
    """Map GitLab usernames to numeric user ids (the write APIs take ids).

    A leading ``@`` is ignored and an all-digit entry is taken as an id already.
    Returns ``None`` when ``usernames`` is ``None`` (field left unchanged) and
    ``[]`` for an empty list (field cleared). Raises :class:`GitlabError` naming
    any username GitLab doesn't know.
    """
    if usernames is None:
        return None
    ids: list[int] = []
    for raw in usernames:
        name = raw.strip().lstrip("@")
        if not name:
            continue
        if name.isdigit():
            ids.append(int(name))
            continue
        matches = gitlab_request("GET", "/users", config=config, params={"username": name})
        if not matches:
            raise GitlabError(f"Unknown GitLab user {name!r}")
        ids.append(int(matches[0]["id"]))
    return ids

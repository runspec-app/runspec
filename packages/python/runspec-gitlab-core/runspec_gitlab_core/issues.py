"""issues.py — project issue operations (GitLab REST v4): reads + writes."""

from __future__ import annotations

from typing import Any

from runspec_gitlab_core.client import GitlabConfig, gitlab_request
from runspec_gitlab_core.projects import _project, resolve_user_ids


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


def _labels(labels: list[str] | None) -> str | None:
    """Render a label list as GitLab's comma-separated form (``None`` = unset)."""
    if labels is None:
        return None
    return ",".join(label.strip() for label in labels if label.strip())


def create_issue(
    config: GitlabConfig,
    title: str,
    description: str | None = None,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    milestone_id: int | None = None,
    due_date: str | None = None,
    confidential: bool = False,
    project: str | None = None,
) -> dict[str, Any]:
    """Create an issue (the canonical ``create_issue`` tool).

    ``assignees`` are usernames (or numeric ids); ``due_date`` is ``YYYY-MM-DD``.
    Returns the created issue.
    """
    return gitlab_request(
        "POST",
        f"/projects/{_project(config, project)}/issues",
        config=config,
        body={
            "title": title,
            "description": description,
            "labels": _labels(labels),
            "assignee_ids": resolve_user_ids(config, assignees),
            "milestone_id": milestone_id,
            "due_date": due_date,
            "confidential": confidential or None,
        },
    )


def update_issue(
    config: GitlabConfig,
    issue_iid: int,
    title: str | None = None,
    description: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    assignees: list[str] | None = None,
    milestone_id: int | None = None,
    due_date: str | None = None,
    state_event: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Edit an issue. Only the fields passed change; ``state_event`` is
    ``close``/``reopen``. Raises :class:`ValueError` when nothing would change."""
    body = {
        "title": title,
        "description": description,
        "add_labels": _labels(add_labels),
        "remove_labels": _labels(remove_labels),
        "assignee_ids": resolve_user_ids(config, assignees),
        "milestone_id": milestone_id,
        "due_date": due_date,
        "state_event": state_event,
    }
    if all(value is None for value in body.values()):
        raise ValueError("Nothing to update — pass at least one field to change")
    return gitlab_request(
        "PUT",
        f"/projects/{_project(config, project)}/issues/{issue_iid}",
        config=config,
        body=body,
    )


def add_issue_note(
    config: GitlabConfig,
    issue_iid: int,
    body: str,
    internal: bool = False,
    project: str | None = None,
) -> dict[str, Any]:
    """Comment on an issue. ``internal`` makes it an internal note (visible to
    project members with at least Reporter access only)."""
    return gitlab_request(
        "POST",
        f"/projects/{_project(config, project)}/issues/{issue_iid}/notes",
        config=config,
        body={"body": body, "internal": internal or None},
    )


def delete_issue(config: GitlabConfig, issue_iid: int, project: str | None = None) -> dict[str, Any]:
    """Delete an issue permanently. GitLab only allows this for project owners
    and administrators."""
    gitlab_request("DELETE", f"/projects/{_project(config, project)}/issues/{issue_iid}", config=config)
    return {"deleted": "issue", "issue_iid": issue_iid}

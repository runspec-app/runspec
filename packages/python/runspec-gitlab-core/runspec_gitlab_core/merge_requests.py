"""merge_requests.py — merge-request operations (GitLab REST v4): reads + writes."""

from __future__ import annotations

from typing import Any

from runspec_gitlab_core.client import GitlabConfig, gitlab_request
from runspec_gitlab_core.issues import _labels
from runspec_gitlab_core.projects import _project, resolve_project_path, resolve_user_ids


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


DRAFT_PREFIX = "Draft: "


def create_merge_request(
    config: GitlabConfig,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str | None = None,
    draft: bool = False,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    reviewers: list[str] | None = None,
    remove_source_branch: bool = False,
    squash: bool = False,
    project: str | None = None,
) -> dict[str, Any]:
    """Open a merge request (the canonical ``create_merge_request`` tool).

    ``draft`` prefixes the title with ``Draft: `` (GitLab's draft marker);
    ``assignees``/``reviewers`` are usernames (or numeric ids).
    """
    if draft and not title.lower().startswith(("draft:", "[draft]", "(draft)")):
        title = DRAFT_PREFIX + title
    return gitlab_request(
        "POST",
        f"/projects/{_project(config, project)}/merge_requests",
        config=config,
        body={
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "labels": _labels(labels),
            "assignee_ids": resolve_user_ids(config, assignees),
            "reviewer_ids": resolve_user_ids(config, reviewers),
            "remove_source_branch": remove_source_branch or None,
            "squash": squash or None,
        },
    )


def update_merge_request(
    config: GitlabConfig,
    mr_iid: int,
    title: str | None = None,
    description: str | None = None,
    target_branch: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    assignees: list[str] | None = None,
    reviewers: list[str] | None = None,
    state_event: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Edit a merge request. Only the fields passed change; ``state_event`` is
    ``close``/``reopen``. Raises :class:`ValueError` when nothing would change."""
    body = {
        "title": title,
        "description": description,
        "target_branch": target_branch,
        "add_labels": _labels(add_labels),
        "remove_labels": _labels(remove_labels),
        "assignee_ids": resolve_user_ids(config, assignees),
        "reviewer_ids": resolve_user_ids(config, reviewers),
        "state_event": state_event,
    }
    if all(value is None for value in body.values()):
        raise ValueError("Nothing to update — pass at least one field to change")
    return gitlab_request(
        "PUT",
        f"/projects/{_project(config, project)}/merge_requests/{mr_iid}",
        config=config,
        body=body,
    )


def add_merge_request_note(
    config: GitlabConfig,
    mr_iid: int,
    body: str,
    internal: bool = False,
    project: str | None = None,
) -> dict[str, Any]:
    """Comment on a merge request (a general, non-diff note)."""
    return gitlab_request(
        "POST",
        f"/projects/{_project(config, project)}/merge_requests/{mr_iid}/notes",
        config=config,
        body={"body": body, "internal": internal or None},
    )


def merge_merge_request(
    config: GitlabConfig,
    mr_iid: int,
    sha: str | None = None,
    squash: bool | None = None,
    remove_source_branch: bool | None = None,
    when_pipeline_succeeds: bool = False,
    merge_commit_message: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Merge a merge request (or set it to merge when its pipeline succeeds).

    ``sha`` guards the merge: GitLab refuses if the source branch head moved
    since you reviewed it. ``squash``/``remove_source_branch`` left as ``None``
    keep the MR's own settings.
    """
    return gitlab_request(
        "PUT",
        f"/projects/{_project(config, project)}/merge_requests/{mr_iid}/merge",
        config=config,
        body={
            "sha": sha,
            "squash": squash,
            "should_remove_source_branch": remove_source_branch,
            "merge_when_pipeline_succeeds": when_pipeline_succeeds or None,
            "merge_commit_message": merge_commit_message,
        },
    )

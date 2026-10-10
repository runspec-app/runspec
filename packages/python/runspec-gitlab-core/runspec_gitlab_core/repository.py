"""repository.py — repository file, tree + branch operations (GitLab REST v4): reads + writes."""

from __future__ import annotations

import base64
import binascii
from typing import Any
from urllib.parse import quote

from runspec_gitlab_core.client import GitlabConfig, GitlabError, gitlab_request
from runspec_gitlab_core.projects import _project


def get_file(
    config: GitlabConfig,
    file_path: str,
    ref: str = "HEAD",
    project: str | None = None,
) -> dict[str, Any]:
    """Fetch a single repository file at ``ref`` (default ``HEAD`` = the default
    branch's tip).

    The base64 payload GitLab returns is decoded: UTF-8 text is returned under
    ``content``; a file that isn't valid UTF-8 is reported as
    ``{"binary": true, "content_base64": ...}`` instead.
    """
    encoded_path = quote(file_path, safe="")
    result = gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/repository/files/{encoded_path}",
        config=config,
        params={"ref": ref},
    )
    raw = result.get("content")
    if isinstance(raw, str) and result.get("encoding") == "base64":
        try:
            data = base64.b64decode(raw)
            result["content"] = data.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            result.pop("content", None)
            result["content_base64"] = raw
            result["binary"] = True
    return result


def list_tree(
    config: GitlabConfig,
    path: str | None = None,
    ref: str | None = None,
    recursive: bool = False,
    project: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> list[dict[str, Any]]:
    """List a repository tree at ``path`` (default root) and ``ref`` (default
    branch). ``recursive`` walks subdirectories."""
    return gitlab_request(
        "GET",
        f"/projects/{_project(config, project)}/repository/tree",
        config=config,
        params={
            "path": path,
            "ref": ref,
            "recursive": recursive,
            "per_page": per_page,
            "page": page,
        },
    )


def create_branch(
    config: GitlabConfig,
    branch: str,
    ref: str,
    project: str | None = None,
) -> dict[str, Any]:
    """Create ``branch`` from ``ref`` (a branch name, tag, or commit SHA)."""
    return gitlab_request(
        "POST",
        f"/projects/{_project(config, project)}/repository/branches",
        config=config,
        body={"branch": branch, "ref": ref},
    )


def _file_exists(config: GitlabConfig, project: str, encoded_path: str, ref: str) -> bool:
    try:
        gitlab_request(
            "GET",
            f"/projects/{project}/repository/files/{encoded_path}",
            config=config,
            params={"ref": ref},
        )
    except GitlabError as exc:
        if exc.status_code == 404:
            return False
        raise
    return True


def write_file(
    config: GitlabConfig,
    file_path: str,
    content: str,
    branch: str,
    commit_message: str,
    start_branch: str | None = None,
    last_commit_id: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Commit a text file to ``branch``, creating it or replacing its content.

    Whether to create or update is decided by checking the file on the branch
    the commit starts from (``start_branch`` when given — GitLab then creates
    ``branch`` from it — else ``branch``). ``last_commit_id`` guards an update:
    GitLab refuses it if the file changed since that commit. Returns
    ``{"file_path", "branch", "action"}`` where ``action`` is
    ``created``/``updated``.
    """
    proj = _project(config, project)
    encoded_path = quote(file_path, safe="")
    exists = _file_exists(config, proj, encoded_path, start_branch or branch)
    result = gitlab_request(
        "PUT" if exists else "POST",
        f"/projects/{proj}/repository/files/{encoded_path}",
        config=config,
        body={
            "branch": branch,
            "start_branch": start_branch,
            "content": content,
            "commit_message": commit_message,
            "last_commit_id": last_commit_id if exists else None,
        },
    )
    out = dict(result) if isinstance(result, dict) else {"file_path": file_path, "branch": branch}
    out["action"] = "updated" if exists else "created"
    return out


def delete_branch(config: GitlabConfig, branch: str, project: str | None = None) -> dict[str, Any]:
    """Delete a branch. GitLab refuses a protected or default branch."""
    gitlab_request(
        "DELETE",
        f"/projects/{_project(config, project)}/repository/branches/{quote(branch, safe='')}",
        config=config,
    )
    return {"deleted": "branch", "branch": branch}


def delete_file(
    config: GitlabConfig,
    file_path: str,
    branch: str,
    commit_message: str,
    last_commit_id: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Delete a file from ``branch`` in one commit. ``last_commit_id`` guards the
    delete: GitLab refuses it if the file changed since that commit."""
    gitlab_request(
        "DELETE",
        f"/projects/{_project(config, project)}/repository/files/{quote(file_path, safe='')}",
        config=config,
        body={"branch": branch, "commit_message": commit_message, "last_commit_id": last_commit_id},
    )
    return {"deleted": "file", "file_path": file_path, "branch": branch}

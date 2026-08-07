"""repository.py — repository file + tree read operations (GitLab REST v4)."""

from __future__ import annotations

import base64
import binascii
from typing import Any
from urllib.parse import quote

from runspec_gitlab_core.client import GitlabConfig, gitlab_request
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

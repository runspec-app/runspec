"""attachments.py — issue attachment operations.

Binary content is handled path-in / path-out: downloads write files to a
destination directory on the invoking host and return metadata; uploads read a
local file path. (Images are ordinary attachments, so ``list-attachments`` +
``download-attachments`` cover the ``get-issue-images`` case.)
"""

from __future__ import annotations

from typing import Any

from runspec_jira_core.client import build_client, translate_errors
from runspec_jira_core.config import JiraConfig


@translate_errors
def list_attachments(config: JiraConfig, issue_key: str) -> Any:
    """List an issue's attachments (id + filename per attachment)."""
    return build_client(config).get_attachments_ids_from_issue(issue_key)


@translate_errors
def download_attachments(config: JiraConfig, issue_key: str, dest: str) -> dict[str, Any]:
    """Download all of an issue's attachments into the ``dest`` directory."""
    path = build_client(config).download_attachments_from_issue(issue_key, path=dest, cloud=config.cloud)
    return {"issue": issue_key, "path": path or dest, "downloaded": True}


@translate_errors
def add_attachment(config: JiraConfig, issue_key: str, file: str) -> Any:
    """Upload a local file as an attachment on an issue."""
    result = build_client(config).add_attachment(issue_key, file)
    return result if result is not None else {"issue": issue_key, "file": file, "attached": True}


@translate_errors
def remove_attachment(config: JiraConfig, attachment_id: str) -> dict[str, Any]:
    """Delete an attachment by its id."""
    build_client(config).remove_attachment(attachment_id)
    return {"attachment_id": attachment_id, "removed": True}

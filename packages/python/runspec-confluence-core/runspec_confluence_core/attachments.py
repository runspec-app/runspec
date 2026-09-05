"""attachments.py — page attachment operations.

Binary content is handled path-in / path-out: downloads write files to a
destination directory on the invoking host and return metadata; uploads read a
local file path.
"""

from __future__ import annotations

import mimetypes
import os
from typing import Any

from runspec_confluence_core.client import _cap_limit, build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig
from runspec_confluence_core.errors import ConfluenceError


@translate_errors
def get_attachments(config: ConfluenceConfig, page_id: str, *, start: int = 0, limit: int = 50, filename: str | None = None, expand: str | None = None) -> Any:
    """List a page's attachments (optionally filtered by ``filename``) — one page
    of results. ``expand`` requests extra sections (e.g. ``version,history``)."""
    return build_client(config).get_attachments_from_content(page_id, start=start, limit=_cap_limit(limit), filename=filename, expand=expand)


@translate_errors
def download_attachments(config: ConfluenceConfig, page_id: str, dest: str) -> dict[str, Any]:
    """Download all of a page's attachments into the ``dest`` directory."""
    path = build_client(config).download_attachments_from_page(page_id, path=dest)
    return {"page_id": page_id, "path": path or dest, "downloaded": True}


def _existing_attachment_id(client: Any, page_id: str, name: str) -> str | None:
    """Return the id of an attachment already on ``page_id`` named ``name``, or
    ``None``. Best-effort: any lookup failure falls back to the create endpoint
    (which Confluence itself versions by filename), so a transient/unsupported
    listing never blocks the upload."""
    try:
        resp = client.get_attachments_from_content(page_id, filename=name)
    except Exception:
        return None
    results = resp.get("results") if isinstance(resp, dict) else None
    for att in results or []:
        if att.get("title") == name:
            return att.get("id")
    return None


@translate_errors
def upload_attachment(
    config: ConfluenceConfig,
    page_id: str,
    file: str,
    *,
    name: str | None = None,
    content_type: str | None = None,
    comment: str | None = None,
) -> Any:
    """Upload a local file as an attachment on a page (``name`` overrides the
    stored filename).

    When an attachment of the same name already exists, its data is **updated**
    (a new version) via ``POST .../child/attachment/{id}/data``; otherwise a new
    attachment is created via ``POST .../child/attachment``. This bypasses
    ``atlassian-python-api``'s ``attach_file``, whose update path PUTs a
    multipart body to the JSON content endpoint and so returns **415 Unsupported
    Media Type** when the file already exists.
    """
    client = build_client(config)
    resolved = os.path.basename(file) if name is None else str(name).replace("\\", "/").rsplit("/", 1)[-1]
    if not resolved:
        raise ConfluenceError("Attachment name must contain a filename")
    if content_type is None:
        content_type = mimetypes.guess_type(resolved)[0] or "application/octet-stream"
    data = {"comment": comment or f"Uploaded {resolved}.", "minorEdit": "true"}
    headers = {"X-Atlassian-Token": "no-check", "Accept": "application/json"}
    base = f"rest/api/content/{page_id}/child/attachment"
    attachment_id = _existing_attachment_id(client, page_id, resolved)
    path = f"{base}/{attachment_id}/data" if attachment_id else base
    with open(file, "rb") as handle:
        return client.post(path=path, data=data, headers=headers, files={"file": (resolved, handle, content_type)})


@translate_errors
def delete_attachment(config: ConfluenceConfig, page_id: str, filename: str) -> dict[str, Any]:
    """Delete a page attachment by filename."""
    build_client(config).delete_attachment(page_id, filename)
    return {"page_id": page_id, "filename": filename, "deleted": True}

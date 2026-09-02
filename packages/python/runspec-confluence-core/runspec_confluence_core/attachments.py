"""attachments.py — page attachment operations.

Binary content is handled path-in / path-out: downloads write files to a
destination directory on the invoking host and return metadata; uploads read a
local file path.
"""

from __future__ import annotations

from typing import Any

from runspec_confluence_core.client import _cap_limit, build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig


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
    stored filename)."""
    return build_client(config).attach_file(file, name=name, content_type=content_type, page_id=page_id, comment=comment)


@translate_errors
def delete_attachment(config: ConfluenceConfig, page_id: str, filename: str) -> dict[str, Any]:
    """Delete a page attachment by filename."""
    build_client(config).delete_attachment(page_id, filename)
    return {"page_id": page_id, "filename": filename, "deleted": True}

"""pages.py — page read/create/update/delete/move + space listing."""

from __future__ import annotations

from typing import Any

from runspec_confluence_core.client import build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig
from runspec_confluence_core.errors import ConfluenceError
from runspec_confluence_core.mdconvert import markdown_to_storage


def _render_body(body: str | None, representation: str) -> tuple[str | None, str]:
    """Resolve the body + representation, converting Markdown to storage XHTML.

    ``representation="markdown"`` is a runspec-side convenience (not a Confluence
    representation): the body is converted to ``storage`` here.
    """
    if representation == "markdown":
        return (markdown_to_storage(body) if body else body), "storage"
    return body, representation


@translate_errors
def get_page(
    config: ConfluenceConfig,
    *,
    page_id: str | None = None,
    space: str | None = None,
    title: str | None = None,
    expand: str | None = None,
) -> Any:
    """Fetch a page by id, or by space + title. ``expand`` (e.g.
    ``body.storage,version``) controls what is returned."""
    client = build_client(config)
    if page_id:
        return client.get_page_by_id(page_id, expand=expand)
    space = space or config.space
    if space and title:
        return client.get_page_by_title(space, title, expand=expand)
    raise ConfluenceError("get-page needs --page-id, or --space (or CONFLUENCE_SPACE) and --title")


@translate_errors
def get_page_children(config: ConfluenceConfig, page_id: str) -> Any:
    """List a page's immediate child pages."""
    return build_client(config).get_child_pages(page_id)


@translate_errors
def get_page_ancestors(config: ConfluenceConfig, page_id: str) -> Any:
    """List a page's ancestor pages (its breadcrumb, root first)."""
    return build_client(config).get_page_ancestors(page_id)


@translate_errors
def create_page(
    config: ConfluenceConfig,
    space: str,
    title: str,
    body: str,
    *,
    parent_id: str | None = None,
    representation: str = "storage",
) -> Any:
    """Create a page. ``body`` is in ``representation`` format (``storage`` XHTML by
    default, or ``wiki`` / ``editor`` / ``markdown`` — ``markdown`` is converted to
    storage client-side)."""
    rendered, rep = _render_body(body, representation)
    return build_client(config).create_page(space, title, rendered, parent_id=parent_id, representation=rep)


@translate_errors
def update_page(
    config: ConfluenceConfig,
    page_id: str,
    title: str,
    *,
    body: str | None = None,
    parent_id: str | None = None,
    representation: str = "storage",
    version_comment: str | None = None,
    minor_edit: bool = False,
) -> Any:
    """Update a page. ``title`` is required by the API even when only the body
    changes (pass the page's current title). ``representation="markdown"`` converts
    the body to storage client-side."""
    rendered, rep = _render_body(body, representation)
    return build_client(config).update_page(
        page_id,
        title,
        body=rendered,
        parent_id=parent_id,
        representation=rep,
        minor_edit=minor_edit,
        version_comment=version_comment,
    )


@translate_errors
def delete_page(config: ConfluenceConfig, page_id: str, *, recursive: bool = False) -> dict[str, Any]:
    """Delete a page (and, with ``recursive``, its descendants)."""
    build_client(config).remove_page(page_id, recursive=recursive)
    return {"page_id": page_id, "deleted": True}


@translate_errors
def move_page(
    config: ConfluenceConfig,
    space: str,
    page_id: str,
    *,
    target_id: str | None = None,
    target_title: str | None = None,
    position: str = "append",
) -> Any:
    """Move a page relative to a target (``append`` under it, or ``above``/``below``)."""
    return build_client(config).move_page(space, page_id, target_id=target_id, target_title=target_title, position=position)


@translate_errors
def get_space_pages(config: ConfluenceConfig, space: str, *, start: int = 0, limit: int = 50) -> Any:
    """List pages in a space (one page of results)."""
    return build_client(config).get_all_pages_from_space(space, start=start, limit=limit)


@translate_errors
def get_space(config: ConfluenceConfig, space_key: str) -> Any:
    """Fetch a space's metadata."""
    return build_client(config).get_space(space_key)

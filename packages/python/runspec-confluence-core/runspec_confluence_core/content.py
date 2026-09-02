"""content.py — history, labels, comments, and restrictions for a page."""

from __future__ import annotations

from typing import Any

from runspec_confluence_core.client import _cap_limit, build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig


@translate_errors
def get_page_history(config: ConfluenceConfig, page_id: str) -> Any:
    """Return a page's content history (created/last-updated, versions)."""
    return build_client(config).get_content_history(page_id)


@translate_errors
def get_labels(config: ConfluenceConfig, page_id: str) -> Any:
    """List a page's labels."""
    return build_client(config).get_page_labels(page_id)


@translate_errors
def add_label(config: ConfluenceConfig, page_id: str, label: str) -> Any:
    """Add a label to a page."""
    return build_client(config).set_page_label(page_id, label)


@translate_errors
def get_comments(config: ConfluenceConfig, page_id: str, *, start: int = 0, limit: int = 25) -> Any:
    """List a page's comments (rendered bodies) — one page of results."""
    return build_client(config).get_page_comments(page_id, expand="body.view", start=start, limit=_cap_limit(limit))


@translate_errors
def add_comment(config: ConfluenceConfig, page_id: str, text: str) -> Any:
    """Add a comment to a page."""
    return build_client(config).add_comment(page_id, text)


@translate_errors
def get_page_restrictions(config: ConfluenceConfig, page_id: str) -> Any:
    """Return the view/edit restrictions on a page."""
    return build_client(config).get_all_restrictions_for_content(page_id)

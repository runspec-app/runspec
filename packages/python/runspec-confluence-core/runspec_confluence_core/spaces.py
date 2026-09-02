"""spaces.py — space listing + per-space page listing and metadata."""

from __future__ import annotations

from typing import Any

from runspec_confluence_core.client import _cap_limit, build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig


@translate_errors
def list_spaces(config: ConfluenceConfig, *, space_type: str | None = None, start: int = 0, limit: int = 25) -> Any:
    """List spaces (one page of results). ``space_type`` filters to ``global`` or
    ``personal`` when given."""
    return build_client(config).get_all_spaces(space_type=space_type, start=start, limit=_cap_limit(limit))


@translate_errors
def get_space_pages(config: ConfluenceConfig, space: str, *, start: int = 0, limit: int = 50) -> Any:
    """List pages in a space (one page of results)."""
    return build_client(config).get_all_pages_from_space(space, start=start, limit=_cap_limit(limit))


@translate_errors
def get_space(config: ConfluenceConfig, space_key: str) -> Any:
    """Fetch a space's metadata."""
    return build_client(config).get_space(space_key)

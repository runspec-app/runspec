"""spaces.py — space listing + per-space page listing and metadata."""

from __future__ import annotations

import itertools
from typing import Any

from runspec_confluence_core.client import _cap_limit, build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig


@translate_errors
def list_spaces(config: ConfluenceConfig, *, space_type: str | None = None, start: int = 0, limit: int = 25, expand: str | None = None) -> Any:
    """List spaces (one page of results). ``space_type`` filters to ``global`` or
    ``personal`` when given; ``expand`` requests extra sections."""
    capped = _cap_limit(limit)
    spaces = build_client(config).get_all_spaces(space_type=space_type, start=start, limit=capped, expand=expand)
    if isinstance(spaces, (dict, list)) or spaces is None:
        return spaces
    # Some SDK versions page lazily (a generator); read one bounded page so the
    # result is JSON, not "<generator object ...>".
    return {"results": list(itertools.islice(spaces, capped))}


@translate_errors
def get_space_pages(config: ConfluenceConfig, space: str, *, start: int = 0, limit: int = 50, expand: str | None = None) -> dict[str, Any]:
    """List pages in a space (one page of results) as ``{"results": [...]}``.

    ``atlassian-python-api``'s ``get_all_pages_from_space`` returns a **generator**
    that follows next-links across the whole space. We materialise a single bounded
    page here (capped at ``limit``) so the result is JSON-serialisable, does not fan
    out into hundreds of requests, and — because iteration happens inside this
    ``@translate_errors``-wrapped call — surfaces an ``HTTPError`` as a
    :class:`ConfluenceError` rather than letting it escape untranslated on lazy
    iteration by the caller.
    """
    capped = _cap_limit(limit)
    pages = build_client(config).get_all_pages_from_space(space, start=start, limit=capped, expand=expand)
    return {"results": list(itertools.islice(pages, capped))}


@translate_errors
def get_space(config: ConfluenceConfig, space_key: str, *, expand: str | None = None) -> Any:
    """Fetch a space's metadata. ``expand`` requests extra sections; when omitted,
    the SDK's own default (``description.plain,homepage``) is left intact rather
    than clobbered with ``None``."""
    client = build_client(config)
    if expand is None:
        return client.get_space(space_key)
    return client.get_space(space_key, expand=expand)

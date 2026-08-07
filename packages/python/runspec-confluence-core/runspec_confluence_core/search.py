"""search.py — CQL content search."""

from __future__ import annotations

from typing import Any

from runspec_confluence_core.client import build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig


@translate_errors
def search(config: ConfluenceConfig, cql: str, *, start: int = 0, limit: int = 25, expand: str | None = None) -> Any:
    """Search content with CQL (Confluence Query Language), e.g.
    ``type = page AND space = DOCS AND text ~ "onboarding"``. Returns one page."""
    return build_client(config).cql(cql, start=start, limit=limit, expand=expand)

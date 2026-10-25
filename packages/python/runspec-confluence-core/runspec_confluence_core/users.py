"""users.py — current-user identity."""

from __future__ import annotations

from typing import Any

from runspec_confluence_core.client import build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig


@translate_errors
def get_current_user(config: ConfluenceConfig) -> Any:
    """Return the authenticated user (a "whoami" for the configured credentials)."""
    client = build_client(config)
    if config.cloud:
        # atlassian-python-api's Cloud client requests the bare "user/current",
        # which misses the v1 REST root; name the full path like the other ops do.
        return client.get("rest/api/user/current")
    return client.get_current_user()

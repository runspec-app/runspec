"""users.py — current-user identity."""

from __future__ import annotations

from typing import Any

from runspec_confluence_core.client import build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig


@translate_errors
def get_current_user(config: ConfluenceConfig) -> Any:
    """Return the authenticated user (a "whoami" for the configured credentials)."""
    # rest/api/user/current is the v1 endpoint on Cloud and Server/DC alike.
    # (atlassian-python-api 4.x has no get_current_user, and 5.x's Cloud client
    # requests the bare "user/current", which isn't an API path.)
    return build_client(config).get("rest/api/user/current")

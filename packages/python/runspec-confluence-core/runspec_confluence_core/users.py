"""users.py — current-user identity."""

from __future__ import annotations

from typing import Any

from runspec_confluence_core.client import build_client, translate_errors
from runspec_confluence_core.config import ConfluenceConfig


@translate_errors
def get_current_user(config: ConfluenceConfig) -> Any:
    """Return the authenticated user (a "whoami" for the configured credentials)."""
    return build_client(config).get_current_user()

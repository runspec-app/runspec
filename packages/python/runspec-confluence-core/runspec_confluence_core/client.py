"""client.py — build an ``atlassian.Confluence`` client + shared op helpers.

``build_client`` picks exactly one auth mode (Basic when username+api_token are
set, else Bearer PAT). ``@translate_errors`` wraps each operation so the SDK's
``requests`` exceptions surface as a :class:`ConfluenceError` carrying
Confluence's own error message.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

import requests
from atlassian import Confluence

from runspec_confluence_core.config import ConfluenceConfig
from runspec_confluence_core.errors import ConfluenceError

_T = TypeVar("_T")


def _auth_kwargs(config: ConfluenceConfig) -> dict[str, Any]:
    base: dict[str, Any] = {"url": config.url, "cloud": config.cloud, "verify_ssl": config.resolve_verify()}
    if config.proxies:
        base["proxies"] = config.proxies
    if config.retry:
        # Retry transient throttling/outage responses (429/413/503, honouring
        # Retry-After). The SDK's own defaults are unbounded for a CLI/agent
        # (up to 1000 retries / 1800s); the config caps default to a sane
        # ceiling (5 attempts / 30s) and are tunable per ConfluenceConfig.
        base.update(
            backoff_and_retry=True,
            max_backoff_retries=config.max_backoff_retries,
            max_backoff_seconds=config.max_backoff_seconds,
        )
    if config.username and config.api_token:
        base.update(username=config.username, password=config.api_token)
    elif config.personal_token:
        base.update(token=config.personal_token)
    else:
        raise ConfluenceError("ConfluenceConfig has no usable credentials (need personal_token or username+api_token)")
    return base


def build_client(config: ConfluenceConfig) -> Confluence:
    """Construct an ``atlassian.Confluence`` client for ``config``.

    ``verify_ssl`` is resolved to a bool or a CA-bundle path and ``proxies`` (when
    set) is forwarded — ``AtlassianRestAPI`` passes both straight through to
    ``requests``. Unless ``config.retry`` is ``False``, transient responses
    (429/413/503) are retried with exponential backoff, bounded by
    ``config.max_backoff_retries`` / ``config.max_backoff_seconds`` (default
    5 attempts / 30s).
    """
    return Confluence(**_auth_kwargs(config))


def _cap_limit(limit: int, cap: int = 100) -> int:
    """Clamp a page-size ``limit`` to ``[1, cap]``.

    Confluence rejects oversized page sizes; callers pass this through to the SDK
    so a caller-supplied ``limit`` can't exceed the server's ceiling."""
    if limit < 1:
        return 1
    return min(limit, cap)


def _message(exc: requests.HTTPError) -> str:
    """Extract a human-readable message from a Confluence error response."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return f"Confluence request failed: {exc}"
    try:
        body = resp.json()
    except Exception:
        return f"Confluence {resp.status_code}: {resp.text[:500]}"
    if isinstance(body, dict):
        message = body.get("message") or body.get("reason")
        if message:
            return f"Confluence {resp.status_code}: {message}"
    return f"Confluence {resp.status_code}: {str(body)[:500]}"


def translate_errors(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Decorator: turn the SDK's ``requests`` exceptions into ``ConfluenceError``."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        try:
            return fn(*args, **kwargs)
        except requests.HTTPError as exc:
            raise ConfluenceError(_message(exc)) from exc
        except requests.RequestException as exc:
            raise ConfluenceError(f"Confluence request failed: {exc}") from exc

    return wrapper

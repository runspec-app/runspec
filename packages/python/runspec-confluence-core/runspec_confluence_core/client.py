"""client.py — build an ``atlassian.Confluence`` client + shared op helpers.

``build_client`` picks exactly one auth mode (Basic when username+api_token are
set, else Bearer PAT). ``@translate_errors`` wraps each operation so the SDK's
``requests`` exceptions surface as a :class:`ConfluenceError` carrying
Confluence's own error message.
"""

from __future__ import annotations

import functools
import re
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
    client = Confluence(**_auth_kwargs(config))
    if not hasattr(client, "get_page_by_id"):
        # atlassian-python-api 5.x swaps in a Cloud client that lacks the v1
        # methods every op here calls; say so instead of failing per op.
        raise ConfluenceError(f"atlassian-python-api {_sdk_version()} is not supported — install atlassian-python-api<5 (pip install 'atlassian-python-api<5')")
    return client


def _sdk_version() -> str:
    try:
        from importlib.metadata import version

        return version("atlassian-python-api")
    except Exception:
        return "(unknown version)"


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
        text = resp.text or ""
        if _looks_like_html(text):
            return f"Confluence {resp.status_code}: {_html_note(text, getattr(resp, 'url', ''))}"
        return f"Confluence {resp.status_code}: {text[:500]}"
    if isinstance(body, dict):
        message = body.get("message") or body.get("reason")
        if message:
            return f"Confluence {resp.status_code}: {message}"
    return f"Confluence {resp.status_code}: {str(body)[:500]}"


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def _html_note(text: str, url: str = "") -> str:
    """One line for an HTML page where JSON was expected — never the page itself
    (a Confluence web-app page is hundreds of KB of script)."""
    match = re.search(r"<title[^>]*>(.*?)</title>", text[:20000], re.IGNORECASE | re.DOTALL)
    title = " ".join(match.group(1).split())[:120] if match else ""
    where = f" from {url}" if url else ""
    titled = f" (title: {title!r})" if title else ""
    return f"got an HTML page{where}{titled} instead of a REST API response — the request missed the API (check CONFLUENCE_URL; Cloud needs the /wiki base)"


def translate_errors(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Decorator: turn the SDK's ``requests`` exceptions into ``ConfluenceError``.

    The SDK returns a response body it can't parse as JSON as plain text, so an
    HTML page (a web-app URL rather than an API one) is raised here too."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        try:
            result = fn(*args, **kwargs)
        except requests.HTTPError as exc:
            raise ConfluenceError(_message(exc)) from exc
        except requests.RequestException as exc:
            raise ConfluenceError(f"Confluence request failed: {exc}") from exc
        if isinstance(result, str) and _looks_like_html(result):
            raise ConfluenceError(f"Confluence: {_html_note(result)}")
        return result

    return wrapper

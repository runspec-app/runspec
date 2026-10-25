"""client.py — build ``atlassian-python-api`` clients + shared op helpers.

``build_client`` / ``build_servicedesk`` turn a :class:`JiraConfig` into an
``atlassian.Jira`` / ``atlassian.ServiceDesk`` instance, picking exactly one auth
mode (Basic when username+api_token are set, else Bearer PAT) so the SDK never
silently drops the PAT because Basic won.

``@translate_errors`` wraps each operation so the SDK's ``requests`` exceptions
surface as a :class:`JiraError` carrying Jira's own error message. ``parse_json_arg``
parses the JSON-string arguments (create/update fields, comment visibility,
worklog, service-desk field values) at the operation boundary.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable
from typing import Any, TypeVar

import requests
from atlassian import Jira, ServiceDesk

from runspec_jira_core.config import JiraConfig
from runspec_jira_core.errors import JiraError

_T = TypeVar("_T")


def _auth_kwargs(config: JiraConfig) -> dict[str, Any]:
    base: dict[str, Any] = {"url": config.url, "cloud": config.cloud, "verify_ssl": config.verify_ssl}
    if config.username and config.api_token:
        base.update(username=config.username, password=config.api_token)
    elif config.personal_token:
        base.update(token=config.personal_token)
    else:
        raise JiraError("JiraConfig has no usable credentials (need personal_token or username+api_token)")
    return base


def build_client(config: JiraConfig) -> Jira:
    """Construct an ``atlassian.Jira`` client for ``config``."""
    return Jira(**_auth_kwargs(config))


def build_servicedesk(config: JiraConfig) -> ServiceDesk:
    """Construct an ``atlassian.ServiceDesk`` client for ``config`` (JSM verbs)."""
    return ServiceDesk(**_auth_kwargs(config))


def parse_json_arg(name: str, raw: str | None) -> Any:
    """Parse a JSON-string CLI argument, or return ``None`` when unset.

    Raises :class:`JiraError` (not a bare ``JSONDecodeError``) so the wrapper's
    fixed catch tuple reports it as a clean ``{"error": ...}`` payload.
    """
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise JiraError(f"--{name} is not valid JSON: {exc}") from exc


def _message(exc: requests.HTTPError) -> str:
    """Extract a human-readable message from a Jira error response."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return f"Jira request failed: {exc}"
    try:
        body = resp.json()
    except Exception:
        text = resp.text or ""
        if _looks_like_html(text):
            return f"Jira {resp.status_code}: {_html_note(text, getattr(resp, 'url', ''))}"
        return f"Jira {resp.status_code}: {text[:500]}"
    if isinstance(body, dict):
        parts: list[str] = list(body.get("errorMessages") or [])
        errors = body.get("errors")
        if isinstance(errors, dict):
            parts += [f"{k}: {v}" for k, v in errors.items()]
        if parts:
            return f"Jira {resp.status_code}: {'; '.join(map(str, parts))}"
        # servicedeskapi / generic shape
        message = body.get("errorMessage") or body.get("message")
        if message:
            return f"Jira {resp.status_code}: {message}"
    return f"Jira {resp.status_code}: {str(body)[:500]}"


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def _html_note(text: str, url: str = "") -> str:
    """One line for an HTML page where JSON was expected — never the page itself
    (a Jira web-app page is tens of KB of script)."""
    match = re.search(r"<title[^>]*>(.*?)</title>", text[:20000], re.IGNORECASE | re.DOTALL)
    title = " ".join(match.group(1).split())[:120] if match else ""
    where = f" from {url}" if url else ""
    titled = f" (title: {title!r})" if title and title != "(null)" else ""
    return (
        f"got an HTML page{where}{titled} instead of a REST API response — the URL is "
        "not a Jira REST endpoint, or this account or site can't use it (check JIRA_URL, "
        "permissions, and that the product, e.g. Jira Service Management, is enabled)"
    )


def translate_errors(fn: Callable[..., _T]) -> Callable[..., _T]:
    """Decorator: turn the SDK's ``requests`` exceptions into ``JiraError``."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        try:
            return fn(*args, **kwargs)
        except requests.HTTPError as exc:
            raise JiraError(_message(exc)) from exc
        except requests.RequestException as exc:
            raise JiraError(f"Jira request failed: {exc}") from exc

    return wrapper

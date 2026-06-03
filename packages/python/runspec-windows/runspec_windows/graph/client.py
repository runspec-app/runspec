"""client.py — thin authenticated Microsoft Graph GET helper."""

from __future__ import annotations

from typing import Any

from runspec_windows.graph import GraphDepError, GraphError
from runspec_windows.graph.auth import get_token

try:
    import httpx
except ImportError:  # optional [graph] dependency
    httpx = None  # type: ignore[assignment]

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def graph_get(path: str, params: dict | None = None, scopes: list[str] | None = None, timeout: int = 30) -> dict:
    """GET ``GRAPH_BASE + path`` with a bearer token and return parsed JSON.

    Raises ``GraphError`` on a non-2xx response, surfacing the Graph error
    message. ``path`` should start with ``/`` (e.g. ``/me/messages``).
    """
    if httpx is None:
        raise GraphDepError("The 'httpx' package is required for Microsoft Graph runnables. Install with: pip install 'runspec-windows[graph]'")
    token = get_token(scopes)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if params and "$search" in params:
        # Graph requires ConsistencyLevel: eventual for $search on messages.
        headers["ConsistencyLevel"] = "eventual"
    resp = httpx.get(GRAPH_BASE + path, headers=headers, params=params, timeout=timeout)
    if resp.status_code >= 400:
        raise GraphError(_error_message(resp))
    return resp.json()  # type: ignore[no-any-return]


def _error_message(resp: Any) -> str:
    try:
        body = resp.json()
        msg = body.get("error", {}).get("message") or str(body)
    except Exception:
        msg = resp.text[:500]
    return f"Graph {resp.status_code}: {msg}"

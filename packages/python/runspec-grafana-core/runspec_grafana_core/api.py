"""api.py — a read-only escape hatch: GET any Grafana HTTP API path.

For the API surface the dedicated helpers don't cover (org settings, teams,
library panels, plugin settings …). GET only — anything that changes state
goes through a helper with its own autonomy.
"""

from __future__ import annotations

from typing import Any

from runspec_grafana_core.client import GrafanaConfig, GrafanaError, grafana_request


def parse_params(pairs: list[str] | None) -> dict[str, Any]:
    """``["key=value", "tag=a", "tag=b"]`` → ``{"key": "value", "tag": ["a", "b"]}``."""
    out: dict[str, Any] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise GrafanaError(f"Query parameter {pair!r} is not key=value")
        key = key.strip()
        if key in out:
            out[key] = [*(out[key] if isinstance(out[key], list) else [out[key]]), value]
        else:
            out[key] = value
    return out


def api_get(config: GrafanaConfig, path: str, params: list[str] | None = None) -> Any:
    """GET ``path`` (must start with ``/api/``) and return the decoded JSON as-is."""
    path = path.strip()
    if not path.startswith("/api/"):
        raise GrafanaError(f"Path must start with /api/ (got {path!r})")
    if ".." in path.split("?")[0].split("/"):
        raise GrafanaError("Path may not contain '..'")
    return grafana_request("GET", path, config=config, params=parse_params(params))

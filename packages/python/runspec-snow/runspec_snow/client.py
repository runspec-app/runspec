"""client.py — authenticated ServiceNow REST helper.

This module is the reusable core of ``runspec-snow``. Every operation function in
the package takes an explicit :class:`SnowConfig` as its first argument, so a
private (e.g. Nexus-hosted) wrapper package can construct a config from injected
credentials and call the same functions the CLI runnables call.

Auth is environment-variable driven (see :func:`config_from_env`) but a caller is
free to build a :class:`SnowConfig` by hand. An OAuth/Bearer token takes
precedence over HTTP Basic (username + password).
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any

import httpx

VALID_ENVS = ("dev", "uat", "prod")


class SnowError(Exception):
    """Raised on configuration problems and non-2xx API responses."""


@dataclass
class SnowConfig:
    """Connection + identity for a ServiceNow instance.

    ``default_table`` is an optional table a wrapper package can bake in once so
    every record operation inherits it (e.g. "this deployment only touches
    ``sc_task``"). ``env`` records which logical environment (dev/uat/prod) the
    config targets, when one was selected.
    """

    base_url: str
    token: str | None = None
    user: str | None = None
    password: str | None = None
    default_table: str | None = None
    env: str | None = None


def _env_first(names: list[str]) -> str | None:
    """Return the first non-empty environment variable among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def config_from_env(env: str | None = None) -> SnowConfig:
    """Build a :class:`SnowConfig` from environment variables.

    When ``env`` (or the ``SNOW_ENV`` variable) selects dev/uat/prod, the
    environment-prefixed variables are read first (e.g. ``SNOW_UAT_BASE_URL``),
    falling back to the unprefixed ``SNOW_BASE_URL`` family for single-instance
    setups. The instance URL may be given as ``SNOW_BASE_URL`` or ``SNOW_INSTANCE``
    (e.g. ``https://acme.service-now.com``). A Bearer token (``SNOW_TOKEN``) wins
    over Basic (``SNOW_USER`` + ``SNOW_PASSWORD``). Raises :class:`SnowError` with
    an actionable message when the env label is invalid or required values are
    missing.
    """
    env = env or os.environ.get("SNOW_ENV")
    if env is not None:
        env = env.lower()
        if env not in VALID_ENVS:
            raise SnowError(f"Invalid env {env!r} — expected one of {', '.join(VALID_ENVS)}")

    # For each field, prefer the env-prefixed variable, then the unprefixed one.
    def lookup(suffix: str) -> str | None:
        names = []
        if env:
            names.append(f"SNOW_{env.upper()}_{suffix}")
        names.append(f"SNOW_{suffix}")
        return _env_first(names)

    base_url = lookup("BASE_URL") or lookup("INSTANCE")
    if not base_url:
        hint = f"SNOW_{env.upper()}_BASE_URL" if env else "SNOW_BASE_URL"
        raise SnowError(f"No instance URL configured — set {hint} (or SNOW_INSTANCE)")

    token = lookup("TOKEN")
    user = lookup("USER")
    password = lookup("PASSWORD")
    if not token and not (user and password):
        if env:
            raise SnowError(f"No credentials configured — set SNOW_{env.upper()}_TOKEN or SNOW_{env.upper()}_USER + SNOW_{env.upper()}_PASSWORD")
        raise SnowError("No credentials configured — set SNOW_TOKEN or SNOW_USER + SNOW_PASSWORD")

    return SnowConfig(
        base_url=base_url.rstrip("/"),
        token=token,
        user=user,
        password=password,
        default_table=lookup("TABLE"),
        env=env,
    )


def _auth_header(config: SnowConfig) -> dict[str, str]:
    """Build the Authorization header — Bearer token preferred, else Basic."""
    if config.token:
        return {"Authorization": f"Bearer {config.token}"}
    if config.user and config.password:
        token = base64.b64encode(f"{config.user}:{config.password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    raise SnowError("SnowConfig has no usable credentials (need token or user+password)")


def resolve_table(config: SnowConfig, table: str | None) -> str:
    """Return ``table`` or fall back to ``config.default_table``; raise if neither."""
    resolved = table or config.default_table
    if not resolved:
        raise SnowError("No table specified — pass --table or set SNOW_TABLE (e.g. incident, sc_task, sc_req_item)")
    return resolved


def snow_request(
    method: str,
    path: str,
    *,
    config: SnowConfig,
    json_body: Any | None = None,
    params: dict[str, Any] | None = None,
    timeout: int = 30,
) -> Any:
    """Make an authenticated request and return parsed JSON (``None`` for 204).

    ``path`` should start with ``/`` (e.g. ``/api/now/table/incident``). Table API
    responses are wrapped in a ``{"result": ...}`` envelope — the operation
    helpers unwrap it. Raises :class:`SnowError` on any non-2xx response,
    surfacing the server's error message.
    """
    headers = {"Accept": "application/json", **_auth_header(config)}
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    resp = httpx.request(
        method,
        config.base_url + path,
        headers=headers,
        json=json_body,
        params=params,
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise SnowError(_error_message(resp))
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


def _error_message(resp: httpx.Response) -> str:
    """Extract a human-readable message from a ServiceNow error response body.

    ServiceNow returns ``{"error": {"message": "...", "detail": "..."}, "status":
    "failure"}``. Surface message + detail when present.
    """
    try:
        body = resp.json()
    except Exception:
        return f"ServiceNow {resp.status_code}: {resp.text[:500]}"

    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message", "")).strip()
            detail = str(error.get("detail", "")).strip()
            text = "; ".join(p for p in (message, detail) if p)
            if text:
                return f"ServiceNow {resp.status_code}: {text}"
    return f"ServiceNow {resp.status_code}: {str(body)[:500]}"

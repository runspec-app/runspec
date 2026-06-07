"""client.py — authenticated Jira Service Management (Data Center) REST helper.

This module is the reusable core of ``runspec-jsm``. Every operation function in
the package takes an explicit :class:`JsmConfig` as its first argument, so a
private (e.g. Nexus-hosted) wrapper package can construct a config from injected
credentials and call the same functions the CLI runnables call.

Auth is environment-variable driven (see :func:`config_from_env`) but a caller is
free to build a :class:`JsmConfig` by hand. PAT (Bearer) takes precedence over
HTTP Basic (username + password).
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any

import httpx

VALID_ENVS = ("dev", "uat", "prod")


class JsmError(Exception):
    """Raised on configuration problems and non-2xx API responses."""


@dataclass
class JsmConfig:
    """Connection + identity for a JSM Data Center instance.

    ``service_desk_id`` / ``project_key`` are optional defaults a wrapper package
    can bake in once so every call inherits them. ``env`` records which logical
    environment (dev/uat/prod) the config targets, when one was selected.
    """

    base_url: str
    pat: str | None = None
    user: str | None = None
    password: str | None = None
    service_desk_id: str | None = None
    project_key: str | None = None
    env: str | None = None


def _env_first(names: list[str]) -> str | None:
    """Return the first non-empty environment variable among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def config_from_env(env: str | None = None) -> JsmConfig:
    """Build a :class:`JsmConfig` from environment variables.

    When ``env`` (or the ``JSM_ENV`` variable) selects dev/uat/prod, the
    environment-prefixed variables are read first (e.g. ``JSM_UAT_BASE_URL``),
    falling back to the unprefixed ``JSM_BASE_URL`` family for single-instance
    setups. PAT wins over Basic. Raises :class:`JsmError` with an actionable
    message when the env label is invalid or required values are missing.
    """
    env = env or os.environ.get("JSM_ENV")
    if env is not None:
        env = env.lower()
        if env not in VALID_ENVS:
            raise JsmError(f"Invalid env {env!r} — expected one of {', '.join(VALID_ENVS)}")

    # For each field, prefer the env-prefixed variable, then the unprefixed one.
    def lookup(suffix: str) -> str | None:
        names = []
        if env:
            names.append(f"JSM_{env.upper()}_{suffix}")
        names.append(f"JSM_{suffix}")
        return _env_first(names)

    base_url = lookup("BASE_URL")
    if not base_url:
        hint = f"JSM_{env.upper()}_BASE_URL" if env else "JSM_BASE_URL"
        raise JsmError(f"No base URL configured — set {hint}")

    pat = lookup("PAT")
    user = lookup("USER")
    password = lookup("PASSWORD")
    if not pat and not (user and password):
        if env:
            raise JsmError(f"No credentials configured — set JSM_{env.upper()}_PAT or JSM_{env.upper()}_USER + JSM_{env.upper()}_PASSWORD")
        raise JsmError("No credentials configured — set JSM_PAT or JSM_USER + JSM_PASSWORD")

    return JsmConfig(
        base_url=base_url.rstrip("/"),
        pat=pat,
        user=user,
        password=password,
        service_desk_id=lookup("SERVICE_DESK_ID"),
        project_key=lookup("PROJECT_KEY"),
        env=env,
    )


def _auth_header(config: JsmConfig) -> dict[str, str]:
    """Build the Authorization header — Bearer PAT preferred, else Basic."""
    if config.pat:
        return {"Authorization": f"Bearer {config.pat}"}
    if config.user and config.password:
        token = base64.b64encode(f"{config.user}:{config.password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    raise JsmError("JsmConfig has no usable credentials (need pat or user+password)")


def jsm_request(
    method: str,
    path: str,
    *,
    config: JsmConfig,
    json_body: Any | None = None,
    params: dict[str, Any] | None = None,
    timeout: int = 30,
) -> Any:
    """Make an authenticated request and return parsed JSON (``None`` for 204).

    ``path`` should start with ``/`` (e.g. ``/rest/api/2/issue/ABC-1``). Raises
    :class:`JsmError` on any non-2xx response, surfacing the server's error
    message.
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
        raise JsmError(_error_message(resp))
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


def _error_message(resp: httpx.Response) -> str:
    """Extract a human-readable message from a Jira/JSM error response body."""
    try:
        body = resp.json()
    except Exception:
        return f"JSM {resp.status_code}: {resp.text[:500]}"

    # Jira returns errorMessages[] and/or errors{}; servicedeskapi returns
    # {errorMessage: "..."}. Surface whatever is present.
    if isinstance(body, dict):
        parts: list[str] = []
        msgs = body.get("errorMessages")
        if isinstance(msgs, list):
            parts.extend(str(m) for m in msgs)
        errors = body.get("errors")
        if isinstance(errors, dict):
            parts.extend(f"{k}: {v}" for k, v in errors.items())
        single = body.get("errorMessage") or body.get("message")
        if single:
            parts.append(str(single))
        if parts:
            return f"JSM {resp.status_code}: {'; '.join(parts)}"
    return f"JSM {resp.status_code}: {str(body)[:500]}"

"""client.py — authenticated GitLab REST (v4) helper.

This module is the reusable core of ``runspec-gitlab``. Every operation function
in the package takes an explicit :class:`GitlabConfig` as its first argument, so a
private (e.g. Nexus-hosted) wrapper package can construct a config from injected
credentials and call the same functions the CLI runnable's verbs call.

Auth is environment-variable driven (see :func:`config_from_env`) but a caller is
free to build a :class:`GitlabConfig` by hand. A single personal-access token
(``GITLAB_TOKEN`` or ``GITLAB_PAT``) is sent as the ``PRIVATE-TOKEN`` header; the instance URL
(``GITLAB_URL``) defaults to ``https://gitlab.com`` for the SaaS case.

This mirrors the read tools of GitLab's own (hosted) MCP server, but ports them
onto the public REST API rather than proxying that server — so the runnable runs
over the runspec-console SSH fleet with no extra process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

VALID_ENVS = ("dev", "uat", "prod")
DEFAULT_URL = "https://gitlab.com"


class GitlabError(Exception):
    """Raised on configuration problems and non-2xx API responses.

    ``status_code`` carries the HTTP status for an API error (``None`` for a
    configuration problem), so a caller can tell e.g. a 404 from a 403.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class GitlabConfig:
    """Connection + identity for a GitLab instance.

    ``project`` is an optional default a wrapper package can bake in once so
    every call inherits it (accepts a numeric id or a ``namespace/project``
    path). ``env`` records which logical environment (dev/uat/prod) the config
    targets, when one was selected.
    """

    base_url: str
    token: str
    project: str | None = None
    env: str | None = None


def _env_first(names: list[str]) -> str | None:
    """Return the first non-empty environment variable among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def config_from_env(env: str | None = None, *, url: str | None = None, token: str | None = None) -> GitlabConfig:
    """Build a :class:`GitlabConfig` from environment variables.

    When ``env`` (or the ``GITLAB_ENV`` variable) selects dev/uat/prod, the
    environment-prefixed variables are read first (e.g. ``GITLAB_UAT_TOKEN``),
    falling back to the unprefixed ``GITLAB_*`` family for single-instance
    setups. The token is read from ``GITLAB_TOKEN`` or ``GITLAB_PAT`` (the two
    names a runspec-console token credential labelled ``GitLab`` sets).
    ``GITLAB_URL`` defaults to ``https://gitlab.com`` when unset. Explicit
    ``url`` / ``token`` arguments win over the environment. Raises
    :class:`GitlabError` with an actionable message when the env label is invalid
    or the token is missing.
    """
    env = env or os.environ.get("GITLAB_ENV")
    if env is not None:
        env = env.lower()
        if env not in VALID_ENVS:
            raise GitlabError(f"Invalid env {env!r} — expected one of {', '.join(VALID_ENVS)}")

    def lookup(*suffixes: str) -> str | None:
        names = []
        if env:
            names += [f"GITLAB_{env.upper()}_{suffix}" for suffix in suffixes]
        names += [f"GITLAB_{suffix}" for suffix in suffixes]
        return _env_first(names)

    token = token or lookup("TOKEN", "PAT")
    if not token:
        hint = f"GITLAB_{env.upper()}_TOKEN" if env else "GITLAB_TOKEN"
        raise GitlabError(f"No token configured — set {hint} (a GitLab personal access token), or add a runspec-console token credential labelled GitLab")

    base_url = url or lookup("URL") or DEFAULT_URL

    return GitlabConfig(
        base_url=base_url.rstrip("/"),
        token=token,
        project=lookup("PROJECT"),
        env=env,
    )


def encode_project(project: str) -> str:
    """URL-encode a project identifier for the ``/projects/:id`` path.

    A ``namespace/project`` path is percent-encoded (slashes included, as GitLab
    requires); a numeric id passes through unchanged.
    """
    return quote(str(project), safe="")


def gitlab_request(
    method: str,
    path: str,
    *,
    config: GitlabConfig,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    raw: bool = False,
    api: bool = True,
    timeout: int = 30,
) -> Any:
    """Make an authenticated request and return the parsed JSON body (or the raw
    text when ``raw=True`` — used for job traces).

    By default the request targets the public REST API at ``{base_url}/api/v4``.
    Pass ``api=False`` to hit ``{base_url}{path}`` directly — used only for the
    non-public web endpoints GitLab does not expose under ``/api/v4`` (e.g. the
    merge-request conflicts endpoint).

    ``path`` should start with ``/`` (e.g. ``/projects/1/issues``). ``None`` values
    in ``params`` are dropped and booleans are rendered as ``"true"``/``"false"``.
    ``body`` is sent as a JSON request body (write operations); its ``None``
    values are dropped too, so callers can pass every optional field.
    Raises :class:`GitlabError` on any non-2xx response, surfacing the server's
    error message.
    """
    headers = {"PRIVATE-TOKEN": config.token}
    if not raw:
        headers["Accept"] = "application/json"

    clean: dict[str, Any] | None = None
    if params:
        clean = {}
        for key, value in params.items():
            if value is None:
                continue
            clean[key] = "true" if value is True else "false" if value is False else value

    prefix = "/api/v4" if api else ""
    resp = httpx.request(
        method,
        f"{config.base_url}{prefix}{path}",
        headers=headers,
        params=clean,
        json={k: v for k, v in body.items() if v is not None} if body is not None else None,
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise GitlabError(_error_message(resp), status_code=resp.status_code)
    if resp.status_code == 204 or not resp.content:
        return "" if raw else None
    if raw:
        return resp.text
    return resp.json()


def _error_message(resp: httpx.Response) -> str:
    """Extract a human-readable message from a GitLab error response body."""
    try:
        body = resp.json()
    except Exception:
        return f"GitLab {resp.status_code}: {resp.text[:500]}"

    if isinstance(body, dict):
        # GitLab returns {"message": "..."} or {"message": {field: [errs]}},
        # and OAuth-style {"error": "...", "error_description": "..."}.
        message = body.get("message")
        if isinstance(message, str):
            return f"GitLab {resp.status_code}: {message}"
        if isinstance(message, dict):
            parts = [f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in message.items()]
            return f"GitLab {resp.status_code}: {'; '.join(parts)}"
        error = body.get("error_description") or body.get("error")
        if error:
            return f"GitLab {resp.status_code}: {error}"
    return f"GitLab {resp.status_code}: {str(body)[:500]}"

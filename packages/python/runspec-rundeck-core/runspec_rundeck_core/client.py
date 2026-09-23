"""client.py — Rundeck API configuration + the shared HTTP request helper.

The error class, the `RundeckConfig` dataclass, `config_from_env`, and the
`rundeck_request` httpx helper all live here. Every op in `jobs.py` / `projects.py`
takes a `RundeckConfig` first arg and calls `rundeck_request`, so a corporate
wrapper can inject credentials / bake in an instance and reuse the ops unchanged.

Targets the Rundeck 4.x REST API. Auth is a Rundeck API token sent in the
``X-Rundeck-Auth-Token`` header. `config_from_env` reads ``RUNDECK_URL`` /
``RUNDECK_TOKEN`` / ``RUNDECK_PROJECT`` (with optional ``RUNDECK_<ENV>_*``
prefixes selected by a free-form env label — dev/uat/prod, or regional labels
like ``prod-eu``), so a CI matrix can push to one instance per job.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

# Rundeck 4.x speaks API v41+. Import/list/delete are stable well below this, so
# v41 is a safe floor for Rundeck 4.17.6 (and configurable via api_version /
# RUNDECK_API_VERSION for older or newer servers).
DEFAULT_API_VERSION = 41


class RundeckError(Exception):
    """Raised on configuration problems and non-2xx API responses."""


@dataclass
class RundeckConfig:
    """A resolved Rundeck target: instance URL, token, and default project.

    ``ca_bundle`` verifies TLS against a private CA / TLS-intercepting proxy;
    ``tls_verify = False`` disables verification entirely; ``proxy`` is an
    explicit ``http://host:port`` egress (else httpx honours the machine's proxy
    environment). ``env`` records the label the config was resolved under.
    """

    url: str
    token: str
    project: str | None = None
    api_version: int = DEFAULT_API_VERSION
    ca_bundle: str | None = None
    tls_verify: bool = True
    proxy: str | None = None
    env: str | None = None
    timeout: float = 30.0

    def api(self, path: str) -> str:
        """Absolute URL for an API path, e.g. ``/project/x/jobs`` → ``…/api/41/project/x/jobs``."""
        return f"{self.url}/api/{self.api_version}{path}"

    def require_project(self, project: str | None = None) -> str:
        """The explicit project or the config default, raising if neither is set."""
        resolved = project or self.project
        if not resolved:
            raise RundeckError("No project configured — pass --project or set RUNDECK_PROJECT.")
        return resolved


def _env_first(names: list[str]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def config_from_env(
    env: str | None = None,
    *,
    url: str | None = None,
    token: str | None = None,
    project: str | None = None,
    api_version: int | None = None,
    ca_bundle: str | None = None,
    proxy: str | None = None,
    tls_verify: bool | None = None,
) -> RundeckConfig:
    """Build a `RundeckConfig` from the environment, with explicit overrides winning.

    Looks up ``RUNDECK_<ENV>_<KEY>`` first (when an env label is given) then the
    unprefixed ``RUNDECK_<KEY>``, for KEY in URL / TOKEN / PROJECT / API_VERSION /
    CA_BUNDLE / PROXY. The env label is free-form (any instance label), so unlike
    a fixed dev/uat/prod set it also carries regional labels. Any keyword override
    (e.g. a ``--url`` / ``--token`` CLI arg) takes precedence over the environment
    and participates in the missing-value check. Raises if no URL or token
    resolves — the two things a push can't proceed without.
    """
    env = env or os.environ.get("RUNDECK_ENV")
    if env:
        env = env.strip()

    def lookup(suffix: str) -> str | None:
        names: list[str] = []
        if env:
            names.append(f"RUNDECK_{env.upper().replace('-', '_')}_{suffix}")
        names.append(f"RUNDECK_{suffix}")
        return _env_first(names)

    url = url or lookup("URL")
    if not url:
        hint = f"RUNDECK_{env.upper().replace('-', '_')}_URL" if env else "RUNDECK_URL"
        raise RundeckError(f"No Rundeck URL configured — pass --url or set {hint} (e.g. https://rundeck.example.com).")

    token = token or lookup("TOKEN")
    if not token:
        hint = f"RUNDECK_{env.upper().replace('-', '_')}_TOKEN" if env else "RUNDECK_TOKEN"
        raise RundeckError(f"No API token configured — pass --token or set {hint} (a Rundeck API token).")

    resolved_version = api_version
    if resolved_version is None:
        raw_version = lookup("API_VERSION")
        if raw_version:
            try:
                resolved_version = int(raw_version)
            except ValueError as exc:
                raise RundeckError(f"Invalid RUNDECK_API_VERSION {raw_version!r} — expected an integer.") from exc
    if resolved_version is None:
        resolved_version = DEFAULT_API_VERSION

    return RundeckConfig(
        url=url.rstrip("/"),
        token=token,
        project=project or lookup("PROJECT"),
        api_version=resolved_version,
        ca_bundle=ca_bundle or lookup("CA_BUNDLE"),
        tls_verify=True if tls_verify is None else tls_verify,
        proxy=proxy or lookup("PROXY"),
        env=env,
    )


def _verify_arg(config: RundeckConfig) -> Any:
    """httpx `verify`: a CA bundle path wins, else the boolean toggle."""
    if config.ca_bundle:
        return config.ca_bundle
    return config.tls_verify


def _error_message(resp: httpx.Response) -> str:
    """Pull a human-readable message out of a Rundeck error response."""
    try:
        data = resp.json()
    except ValueError:
        text = resp.text.strip()
        return f"HTTP {resp.status_code}: {text[:400]}" if text else f"HTTP {resp.status_code}"
    if isinstance(data, dict):
        message = data.get("message") or data.get("error")
        if message:
            return f"HTTP {resp.status_code}: {message}"
    return f"HTTP {resp.status_code}: {resp.text[:400]}"


def rundeck_request(
    method: str,
    path: str,
    *,
    config: RundeckConfig,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    content: bytes | str | None = None,
    content_type: str | None = None,
    expect_json: bool = True,
) -> Any:
    """Issue one Rundeck API request and return the parsed body.

    ``path`` is API-relative (``/project/x/jobs``). Drops ``None`` params. Sends
    ``json_body`` as JSON, or raw ``content`` under ``content_type`` (used for the
    job-import body). Raises `RundeckError` on any status ≥ 400; returns ``None``
    for an empty / 204 body, else the parsed JSON (or text when ``expect_json`` is
    False).
    """
    headers = {"X-Rundeck-Auth-Token": config.token}
    if expect_json:
        headers["Accept"] = "application/json"
    if content_type:
        headers["Content-Type"] = content_type

    clean: dict[str, Any] | None = None
    if params:
        clean = {}
        for key, value in params.items():
            if value is None:
                continue
            clean[key] = "true" if value is True else "false" if value is False else value

    kwargs: dict[str, Any] = {
        "headers": headers,
        "params": clean,
        "timeout": config.timeout,
        "verify": _verify_arg(config),
        "follow_redirects": True,
    }
    if config.proxy:
        kwargs["proxy"] = config.proxy
    if json_body is not None:
        kwargs["json"] = json_body
    if content is not None:
        kwargs["content"] = content

    resp = httpx.request(method, config.api(path), **kwargs)
    if resp.status_code >= 400:
        raise RundeckError(_error_message(resp))
    if resp.status_code == 204 or not resp.content:
        return None
    if not expect_json:
        return resp.text
    return resp.json()

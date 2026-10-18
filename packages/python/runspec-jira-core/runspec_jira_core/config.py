"""config.py — connection + identity for a Jira instance, from the environment.

``JiraConfig`` carries everything the client builder needs. ``config_from_env``
reads the ``JIRA_*`` family (with an optional ``JIRA_<ENV>_*`` prefix selected by
``--env``), exactly like ``runspec-jsm`` / ``runspec-snow``. Every operation
function in the package takes an explicit ``JiraConfig`` first argument, so a
private (e.g. Nexus-hosted) wrapper can inject credentials in code, bake in a
default ``service_desk_id`` / ``project_key`` / ``env``, and build "one runnable
per project" on top.

Cloud vs Server/Data Center is resolved once: an explicit ``JIRA_CLOUD`` wins,
otherwise a ``.atlassian.net`` host is treated as Cloud and anything else as
Server/DC. This drives both the ``atlassian.Jira(cloud=...)`` flag and the
Cloud-vs-DC branches in a few operations (user identity, assignment, watchers).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from runspec_jira_core.errors import JiraError

VALID_ENVS = ("dev", "uat", "prod")

_FALSEY = ("0", "false", "no", "off")
_TRUTHY = ("1", "true", "yes", "on")


@dataclass
class JiraConfig:
    """Connection + identity for a Jira instance.

    Provide EITHER ``personal_token`` (a Server/Data Center Personal Access Token,
    sent as a Bearer token) OR ``username`` + ``api_token`` (Cloud email + API
    token, or a Server/DC basic username + password). ``service_desk_id`` and
    ``project_key`` are optional defaults a wrapper can bake in.
    """

    url: str
    cloud: bool
    username: str | None = None
    api_token: str | None = None
    personal_token: str | None = None
    service_desk_id: str | None = None
    project_key: str | None = None
    verify_ssl: bool = True
    env: str | None = None


def _env_first(names: list[str]) -> str | None:
    """Return the first non-empty environment variable among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _resolve_cloud(url: str, override: str | None) -> bool:
    """Decide Cloud vs Server/DC: explicit override wins, else infer from host."""
    if override is not None:
        return override.strip().lower() in _TRUTHY
    return ".atlassian.net" in url.lower()


def config_from_env(
    env: str | None = None,
    *,
    personal_token_override: str | None = None,
    url: str | None = None,
    username: str | None = None,
    api_token: str | None = None,
) -> JiraConfig:
    """Build a :class:`JiraConfig` from environment variables.

    When ``env`` (or the ``JIRA_ENV`` variable) selects dev/uat/prod, the
    environment-prefixed variables are read first (e.g. ``JIRA_UAT_URL``), falling
    back to the unprefixed ``JIRA_*`` family for single-instance setups. Raises
    :class:`JiraError` with an actionable message when the env label is invalid,
    the base URL is missing, or no usable credentials are configured.

    ``personal_token_override`` — when set (e.g. from the ``--pat`` secret arg
    supplied through runspec-console) — is used as the PAT and is authoritative:
    it satisfies the credential requirement on its own and takes precedence over
    any env-configured Basic credentials. The base URL is still read from the
    environment unless ``url`` is given.

    ``url`` / ``username`` / ``api_token`` override the matching variables.
    Each variable also has the spelling a runspec-console credential sets:
    ``JIRA_USER`` for ``USERNAME``, ``JIRA_PASSWORD`` for ``API_TOKEN``, and
    ``JIRA_PAT`` / ``JIRA_TOKEN`` for ``PERSONAL_TOKEN``.
    """
    env = env or os.environ.get("JIRA_ENV")
    if env is not None:
        env = env.lower()
        if env not in VALID_ENVS:
            raise JiraError(f"Invalid env {env!r} — expected one of {', '.join(VALID_ENVS)}")

    def lookup(*suffixes: str) -> str | None:
        names = []
        if env:
            names += [f"JIRA_{env.upper()}_{suffix}" for suffix in suffixes]
        names += [f"JIRA_{suffix}" for suffix in suffixes]
        return _env_first(names)

    url = url or lookup("URL")
    if not url:
        hint = f"JIRA_{env.upper()}_URL" if env else "JIRA_URL"
        raise JiraError(f"No base URL configured — set {hint}")

    verify = lookup("VERIFY_SSL")
    verify_ssl = True if verify is None else verify.strip().lower() not in _FALSEY
    common: dict[str, Any] = {
        "url": url.rstrip("/"),
        "cloud": _resolve_cloud(url, lookup("CLOUD")),
        "service_desk_id": lookup("SERVICE_DESK_ID"),
        "project_key": lookup("PROJECT_KEY"),
        "verify_ssl": verify_ssl,
        "env": env,
    }

    if personal_token_override:
        return JiraConfig(personal_token=personal_token_override, **common)

    # Basic credentials passed in win over a PAT from the environment.
    explicit_basic = bool(username and api_token)
    username = username or lookup("USERNAME", "USER")
    api_token = api_token or lookup("API_TOKEN", "PASSWORD")
    personal_token = None if explicit_basic else lookup("PERSONAL_TOKEN", "PAT", "TOKEN")
    if not personal_token and not (username and api_token):
        raise JiraError("No credentials configured — set JIRA_PERSONAL_TOKEN (Server/DC PAT) or JIRA_USERNAME + JIRA_API_TOKEN (Cloud email + API token, or DC basic auth)")

    return JiraConfig(username=username, api_token=api_token, personal_token=personal_token, **common)

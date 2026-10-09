"""config.py — connection + identity for a Confluence instance, from the environment.

Mirrors ``runspec-jira-core``'s config: ``config_from_env`` reads the
``CONFLUENCE_*`` family (with an optional ``CONFLUENCE_<ENV>_*`` prefix selected by
``--env``). Every operation function takes an explicit ``ConfluenceConfig`` first
argument, so a private (e.g. Nexus-hosted) wrapper can inject credentials in code
and bake in a default ``space`` / ``env``.

Cloud vs Server/Data Center is resolved once: an explicit ``CONFLUENCE_CLOUD`` wins,
otherwise a ``.atlassian.net`` host is treated as Cloud and anything else as
Server/DC.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from runspec_confluence_core.errors import ConfluenceError

VALID_ENVS = ("dev", "uat", "prod")

_FALSEY = ("0", "false", "no", "off")
_TRUTHY = ("1", "true", "yes", "on")

# Default retry ceiling used when ``retry`` is on. Bounded for a CLI/agent
# context (the SDK's own defaults are ~1000 attempts / 1800s). Importing
# packages can reference these to tune relative to the baseline.
DEFAULT_MAX_BACKOFF_RETRIES = 5
DEFAULT_MAX_BACKOFF_SECONDS = 30


@dataclass
class ConfluenceConfig:
    """Connection + identity for a Confluence instance.

    Provide EITHER ``personal_token`` (a Server/Data Center Personal Access Token,
    sent as a Bearer token) OR ``username`` + ``api_token`` (Cloud email + API
    token, or a Server/DC basic username + password). ``space`` is an optional
    default a wrapper can bake in.
    """

    url: str
    cloud: bool
    username: str | None = None
    api_token: str | None = None
    personal_token: str | None = None
    space: str | None = None
    verify_ssl: bool | str = True
    proxies: dict[str, str] | None = None
    retry: bool = True
    max_backoff_retries: int = DEFAULT_MAX_BACKOFF_RETRIES
    max_backoff_seconds: int = DEFAULT_MAX_BACKOFF_SECONDS
    env: str | None = None
    # Diagram creation (see diagram_write.py). ``kroki_url`` points at a Kroki
    # server that renders diagram text to PNG; ``diagram_mode`` picks "macro"
    # (a Mermaid/PlantUML app's macro) or "image" (rendered PNG attachment) when
    # a call doesn't say — unset means "image" when ``kroki_url`` is set. Both
    # are opt-in: with neither set, a diagram needs a caller-rendered PNG. The
    # macro names are the installed apps' (vendors differ).
    kroki_url: str | None = None
    diagram_mode: str | None = None
    mermaid_macro: str | None = None
    plantuml_macro: str = "plantuml"

    def resolve_verify(self) -> bool | str:
        """Resolve ``verify_ssl`` into the value ``requests`` expects.

        ``requests``' ``verify`` is either a bool (verify against the system CA
        store, or skip verification) or a path string to a CA bundle to verify
        against. A non-empty string is returned as-is (the CA-bundle path); an
        empty string means "use the default", so it resolves to ``True``.
        """
        if isinstance(self.verify_ssl, str):
            return self.verify_ssl or True
        return self.verify_ssl


# Well-known system CA-bundle locations, probed by detect_verify().
_SYSTEM_CA_BUNDLES = (
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL / CentOS / Fedora
    "/etc/ssl/certs/ca-certificates.crt",  # Debian / Ubuntu / Alpine
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/pki/tls/cacert.pem",  # older RHEL
)


def detect_verify() -> bool | str:
    """Best-effort platform trust-store detection, returning a value for
    ``ConfluenceConfig.verify_ssl`` (requests' ``verify``).

    - If ``truststore`` is installed, inject it into ``ssl`` (so the OS trust
      store — Windows cert store, macOS keychain, Linux system bundle — is used)
      and return ``True``.
    - Otherwise, return the first existing well-known system CA-bundle path
      (RHEL / Debian / SUSE locations) — common on corporate Server/DC hosts.
    - Otherwise return ``True`` (fall back to ``certifi``, requests' default).

    It **never returns ``False``**: unlike the implementation this is ported from
    (which silently disables verification when no bundle is found), verification
    stays on and a caller opts out explicitly with ``verify_ssl=False``. A
    deployment can alternatively install ``pip_system_certs`` to route requests
    through the OS store with no call here at all.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
        return True
    except Exception:
        pass
    for path in _SYSTEM_CA_BUNDLES:
        if os.path.isfile(path):
            return path
    return True


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


def _env_int(value: str | None, default: int) -> int:
    """Parse an integer env value, falling back to ``default`` when unset or
    non-numeric."""
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def _resolve_verify_ssl(value: str | None) -> bool | str:
    """Parse ``CONFLUENCE_VERIFY_SSL`` into a bool or a CA-bundle path.

    Unset defaults to ``True``. A falsey/truthy token (``0``/``false``/``no``/
    ``off`` and ``1``/``true``/``yes``/``on``) maps to the bool; any other value
    is treated as a filesystem path to a CA bundle to verify against.
    """
    if value is None:
        return True
    token = value.strip()
    lowered = token.lower()
    if lowered in _FALSEY:
        return False
    if lowered in _TRUTHY:
        return True
    return token


def config_from_env(env: str | None = None, *, personal_token_override: str | None = None) -> ConfluenceConfig:
    """Build a :class:`ConfluenceConfig` from environment variables.

    When ``env`` (or the ``CONFLUENCE_ENV`` variable) selects dev/uat/prod, the
    environment-prefixed variables are read first (e.g. ``CONFLUENCE_UAT_URL``),
    falling back to the unprefixed ``CONFLUENCE_*`` family. Raises
    :class:`ConfluenceError` when the env label is invalid, the base URL is
    missing, or no usable credentials are configured.

    ``personal_token_override`` — when set (e.g. from the ``--pat`` secret arg
    supplied through runspec-console) — is used as the PAT and is authoritative:
    it satisfies the credential requirement on its own and takes precedence over
    any env-configured Basic credentials. The base URL is still read from the
    environment.
    """
    env = env or os.environ.get("CONFLUENCE_ENV")
    if env is not None:
        env = env.lower()
        if env not in VALID_ENVS:
            raise ConfluenceError(f"Invalid env {env!r} — expected one of {', '.join(VALID_ENVS)}")

    def lookup(suffix: str) -> str | None:
        names = []
        if env:
            names.append(f"CONFLUENCE_{env.upper()}_{suffix}")
        names.append(f"CONFLUENCE_{suffix}")
        return _env_first(names)

    url = lookup("URL")
    if not url:
        hint = f"CONFLUENCE_{env.upper()}_URL" if env else "CONFLUENCE_URL"
        raise ConfluenceError(f"No base URL configured — set {hint}")

    proxy_url = lookup("PROXY_URL")
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    retry = lookup("RETRY")
    common: dict[str, Any] = {
        "url": url.rstrip("/"),
        "cloud": _resolve_cloud(url, lookup("CLOUD")),
        "space": lookup("SPACE"),
        "verify_ssl": _resolve_verify_ssl(lookup("VERIFY_SSL")),
        "proxies": proxies,
        "retry": True if retry is None else retry.strip().lower() not in _FALSEY,
        "max_backoff_retries": _env_int(lookup("MAX_BACKOFF_RETRIES"), DEFAULT_MAX_BACKOFF_RETRIES),
        "max_backoff_seconds": _env_int(lookup("MAX_BACKOFF_SECONDS"), DEFAULT_MAX_BACKOFF_SECONDS),
        "env": env,
        "kroki_url": (lookup("KROKI_URL") or "").rstrip("/") or None,
        "diagram_mode": (lookup("DIAGRAM_MODE") or "").strip().lower() or None,
        "mermaid_macro": lookup("MERMAID_MACRO"),
        "plantuml_macro": lookup("PLANTUML_MACRO") or "plantuml",
    }

    if personal_token_override:
        return ConfluenceConfig(personal_token=personal_token_override, **common)

    username = lookup("USERNAME")
    api_token = lookup("API_TOKEN")
    personal_token = lookup("PERSONAL_TOKEN")
    if not personal_token and not (username and api_token):
        raise ConfluenceError("No credentials configured — set CONFLUENCE_PERSONAL_TOKEN (Server/DC PAT) or CONFLUENCE_USERNAME + CONFLUENCE_API_TOKEN (Cloud email + API token, or DC basic auth)")

    return ConfluenceConfig(username=username, api_token=api_token, personal_token=personal_token, **common)

"""client.py — Prometheus connection config, the shared HTTP helper, and time parsing.

Every operation in the package takes an explicit :class:`PrometheusConfig` first
argument, so a private wrapper package can build a config from injected
credentials (or bake in an instance URL / tenant) and call the same functions the
``prometheus`` runnable's verbs call.

Auth is environment-variable driven (see :func:`config_from_env`): a bearer token
(``PROMETHEUS_TOKEN``), or basic auth (``PROMETHEUS_USERNAME`` +
``PROMETHEUS_PASSWORD``), or none (a plain Prometheus on localhost). The URL
defaults to ``http://127.0.0.1:9090`` — the right answer when the runnable runs
on the Prometheus host itself over the console's SSH fleet.

This ports the tools of the official Prometheus MCP server onto the public HTTP
API rather than proxying that server, so no extra process is involved.
"""

from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:9090"
# Range queries with no explicit step aim for about this many points (same as the MCP server).
DEFAULT_RANGE_POINTS = 250


class PrometheusError(Exception):
    """Raised on configuration problems, bad input, and non-2xx / error API responses.

    ``status_code`` carries the HTTP status for an API error (``None`` otherwise);
    ``error_type`` carries Prometheus's own ``errorType`` (e.g. ``bad_data``).
    """

    def __init__(self, message: str, status_code: int | None = None, error_type: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


@dataclass
class PrometheusConfig:
    """Connection + identity for one Prometheus-compatible endpoint.

    Works against Prometheus itself and API-compatible servers (Thanos Query,
    Mimir/Cortex — set ``org_id`` for their ``X-Scope-OrgID`` tenant header).
    ``ca_bundle`` verifies TLS against a private CA; ``tls_verify = False``
    disables verification; ``proxy`` is an explicit egress (else httpx honours
    the machine's proxy environment). ``headers`` are sent on every request.
    """

    url: str = DEFAULT_URL
    token: str | None = None
    username: str | None = None
    password: str | None = None
    org_id: str | None = None
    ca_bundle: str | None = None
    tls_verify: bool = True
    proxy: str | None = None
    env: str | None = None
    timeout: float = 30.0
    headers: dict[str, str] = field(default_factory=dict)


def _env_first(names: list[str]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _env_key(env: str) -> str:
    return env.strip().upper().replace("-", "_")


def config_from_env(
    env: str | None = None,
    *,
    url: str | None = None,
    token: str | None = None,
) -> PrometheusConfig:
    """Build a :class:`PrometheusConfig` from environment variables.

    With an ``env`` label (or ``PROMETHEUS_ENV``) the prefixed variables are read
    first — ``PROMETHEUS_<ENV>_URL`` etc., the label free-form (``prod``,
    ``prod-eu``) — falling back to the unprefixed ``PROMETHEUS_*`` family.
    Keys: ``URL``, ``TOKEN`` (or ``PAT``), ``USERNAME`` (or ``USER``),
    ``PASSWORD``, ``ORG_ID``, ``CA_BUNDLE``, ``TLS_VERIFY`` (``false`` to
    disable), ``PROXY``. Explicit ``url`` / ``token`` arguments win over the
    environment. No credentials is valid (an unsecured Prometheus).
    """
    env = env or os.environ.get("PROMETHEUS_ENV")
    env = env.strip() if env else None

    def lookup(*suffixes: str) -> str | None:
        names: list[str] = []
        for suffix in suffixes:
            if env:
                names.append(f"PROMETHEUS_{_env_key(env)}_{suffix}")
        names += [f"PROMETHEUS_{suffix}" for suffix in suffixes]
        return _env_first(names)

    username = lookup("USERNAME", "USER")
    password = lookup("PASSWORD")
    if bool(username) != bool(password):
        raise PrometheusError("Basic auth needs both PROMETHEUS_USERNAME and PROMETHEUS_PASSWORD (or use PROMETHEUS_TOKEN).")

    verify_raw = lookup("TLS_VERIFY")
    tls_verify = not (verify_raw and verify_raw.strip().lower() in ("0", "false", "no", "off"))

    return PrometheusConfig(
        url=(url or lookup("URL") or DEFAULT_URL).rstrip("/"),
        token=token or lookup("TOKEN", "PAT"),
        username=username,
        password=password,
        org_id=lookup("ORG_ID"),
        ca_bundle=lookup("CA_BUNDLE"),
        tls_verify=tls_verify,
        proxy=lookup("PROXY"),
        env=env,
    )


def _client_kwargs(config: PrometheusConfig) -> dict[str, Any]:
    headers = {"User-Agent": "runspec-prometheus-core", **config.headers}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"
    if config.org_id:
        headers["X-Scope-OrgID"] = config.org_id
    kwargs: dict[str, Any] = {
        "headers": headers,
        "timeout": config.timeout,
        "verify": config.ca_bundle or config.tls_verify,
    }
    if config.username and config.password and not config.token:
        kwargs["auth"] = (config.username, config.password)
    if config.proxy:
        kwargs["proxy"] = config.proxy
    return kwargs


def _clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None or value == [] or value == "":
            continue
        out[key] = "true" if value is True else "false" if value is False else value
    return out or None


def prometheus_request(
    method: str,
    path: str,
    *,
    config: PrometheusConfig,
    params: dict[str, Any] | None = None,
    raw: bool = False,
) -> Any:
    """Make a request against ``{config.url}{path}``.

    For the JSON API (``/api/v1/...``) returns the envelope minus ``status`` —
    ``{"data": ..., "warnings": [...]}`` (``warnings``/``infos`` only when
    present). With ``raw=True`` returns the body text (the ``/-/`` management
    endpoints). ``None``/empty params are dropped; list values repeat the key
    (``match[]``); a POST sends them as a form body. Raises :class:`PrometheusError` on an HTTP error or an
    ``"status": "error"`` envelope, carrying Prometheus's own message.
    """
    with httpx.Client(**_client_kwargs(config)) as client:
        try:
            # POST sends params as a form body (long PromQL stays out of the URL).
            post = method.upper() == "POST"
            resp = client.request(method, f"{config.url}{path}", data=_clean(params) if post else None, params=None if post else _clean(params))
        except httpx.HTTPError as exc:
            raise PrometheusError(f"Cannot reach Prometheus at {config.url}: {exc}") from exc

    if resp.status_code >= 400:
        raise _api_error(resp)
    if raw:
        return resp.text
    if resp.status_code == 204 or not resp.content:
        return {"data": None}
    try:
        body = resp.json()
    except ValueError as exc:
        raise PrometheusError(f"Prometheus returned non-JSON from {path}: {resp.text[:300]}") from exc
    if isinstance(body, dict) and body.get("status") == "error":
        raise PrometheusError(f"Prometheus: {body.get('error', 'unknown error')}", resp.status_code, body.get("errorType"))
    if not isinstance(body, dict) or "data" not in body:
        return {"data": body}
    out = {"data": body["data"]}
    for key in ("warnings", "infos"):
        if body.get(key):
            out[key] = body[key]
    return out


def _api_error(resp: httpx.Response) -> PrometheusError:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("error"):
        return PrometheusError(f"Prometheus {resp.status_code}: {body['error']}", resp.status_code, body.get("errorType"))
    hint = ""
    url = str(resp.request.url)
    if resp.status_code == 404 and url.endswith("/api/v1/stores"):
        hint = " (list-stores only works against Thanos Query)"
    elif resp.status_code in (403, 404) and "/-/" in url:
        hint = " (reload/quit need Prometheus started with --web.enable-lifecycle)"
    elif resp.status_code in (403, 404) and "/admin/" in url:
        hint = " (the TSDB admin API needs Prometheus started with --web.enable-admin-api)"
    elif resp.status_code in (401, 403):
        hint = " (check PROMETHEUS_TOKEN or PROMETHEUS_USERNAME/PASSWORD)"
    return PrometheusError(f"Prometheus {resp.status_code}: {resp.text.strip()[:300]}{hint}", resp.status_code)


# ── time handling ──────────────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^-?((\d+)(ms|y|w|d|h|m|s))+$")
_DURATION_PART = re.compile(r"(\d+)(ms|y|w|d|h|m|s)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


def parse_duration(value: str) -> float:
    """Parse a Prometheus duration (``30s``, ``5m``, ``1h30m``, ``2d``) to seconds.

    A leading ``-`` is ignored (callers mean "that long ago" either way).
    Raises :class:`PrometheusError` when it isn't a duration.
    """
    text = value.strip()
    if not _DURATION_RE.match(text):
        raise PrometheusError(f"Cannot parse {value!r} as a duration (e.g. 30s, 5m, 1h30m, 2d)")
    return float(sum(int(n) * _UNIT_SECONDS[unit] for n, unit in _DURATION_PART.findall(text)))


def parse_time(value: str | float | int | None, *, now: float | None = None) -> float | None:
    """Parse a timestamp to Unix seconds, or ``None`` when ``value`` is empty.

    Accepts Unix seconds (``1733000000``), RFC 3339 (``2026-10-12T09:00:00Z``),
    ``now``, or a duration back from now (``5m``, ``1h30m``) — the same forms
    the MCP server takes.
    """
    if value is None or value == "":
        return None
    current = time.time() if now is None else now
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip()
    if text.lower() == "now":
        return current
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    try:
        return current - parse_duration(text)
    except PrometheusError:
        raise PrometheusError(f"Cannot parse {value!r} as a time — use Unix seconds, RFC 3339, or a duration back from now (e.g. 1h)") from None


def auto_step(start: float, end: float) -> float:
    """A step giving about :data:`DEFAULT_RANGE_POINTS` points, at least 1 second."""
    return float(max(math.floor((end - start) / DEFAULT_RANGE_POINTS), 1))


def iso(ts: float | None) -> str | None:
    """Unix seconds → RFC 3339 UTC (for echoing the resolved window back)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def truncate(items: list[Any], limit: int | None) -> tuple[list[Any], bool]:
    """Cap ``items`` at ``limit`` entries; ``None``/``0``/negative means no cap."""
    if limit is None or limit <= 0 or len(items) <= limit:
        return items, False
    return items[:limit], True

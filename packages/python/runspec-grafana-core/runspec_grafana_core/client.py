"""client.py — Grafana connection config, the shared HTTP helpers, and time parsing.

Every operation in the package takes an explicit :class:`GrafanaConfig` first
argument, so a private wrapper package can build a config from injected
credentials (or bake in an instance URL / org) and call the same functions the
``grafana`` runnable's verbs call.

Auth is environment-variable driven (see :func:`config_from_env`): a service
account token (``GRAFANA_SERVICE_ACCOUNT_TOKEN``, or ``GRAFANA_TOKEN``), or
basic auth (``GRAFANA_USERNAME`` + ``GRAFANA_PASSWORD``). The URL defaults to
``http://127.0.0.1:3000`` — the right answer when the runnable runs on the
Grafana host itself over the console's SSH fleet.

Queries against Prometheus and Loki go through Grafana's datasource proxy
(:func:`datasource_request`), so the datasource's own credentials stay in
Grafana and only the Grafana token is needed. This ports the read tools of the
Grafana MCP server (https://github.com/grafana/mcp-grafana) onto the Grafana
HTTP API rather than proxying that server, so no extra process is involved.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_URL = "http://127.0.0.1:3000"


class GrafanaError(Exception):
    """Raised on configuration problems, bad input, and non-2xx / error API responses.

    ``status_code`` carries the HTTP status for an API error (``None`` otherwise).
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class GrafanaConfig:
    """Connection + identity for one Grafana instance.

    ``token`` is a service account token (sent as a bearer token); basic auth
    (``username`` + ``password``) is the alternative. ``org_id`` selects the
    organization (``X-Grafana-Org-Id``). ``ca_bundle`` verifies TLS against a
    private CA; ``tls_verify = False`` disables verification; ``proxy`` is an
    explicit egress (else httpx honours the machine's proxy environment).
    ``headers`` are sent on every request.
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
    org_id: str | int | None = None,
    username: str | None = None,
    password: str | None = None,
) -> GrafanaConfig:
    """Build a :class:`GrafanaConfig` from environment variables.

    With an ``env`` label (or ``GRAFANA_ENV``) the prefixed variables are read
    first — ``GRAFANA_<ENV>_URL`` etc., the label free-form (``prod``,
    ``prod-eu``) — falling back to the unprefixed ``GRAFANA_*`` family.
    Keys: ``URL``, ``SERVICE_ACCOUNT_TOKEN`` (or ``TOKEN``, ``PAT``,
    ``API_KEY``), ``USERNAME`` (or ``USER``), ``PASSWORD``, ``ORG_ID``,
    ``CA_BUNDLE``, ``TLS_VERIFY`` (``false`` to disable), ``PROXY``. Explicit
    ``url`` / ``token`` / ``org_id`` / ``username`` / ``password`` arguments
    win over the environment.
    No credentials is valid (anonymous access).
    """
    env = env or os.environ.get("GRAFANA_ENV")
    env = env.strip() if env else None

    def lookup(*suffixes: str) -> str | None:
        names: list[str] = []
        for suffix in suffixes:
            if env:
                names.append(f"GRAFANA_{_env_key(env)}_{suffix}")
        names += [f"GRAFANA_{suffix}" for suffix in suffixes]
        return _env_first(names)

    username = username or lookup("USERNAME", "USER")
    password = password or lookup("PASSWORD")
    if bool(username) != bool(password):
        raise GrafanaError("Basic auth needs both GRAFANA_USERNAME and GRAFANA_PASSWORD (or use GRAFANA_SERVICE_ACCOUNT_TOKEN).")

    verify_raw = lookup("TLS_VERIFY")
    tls_verify = not (verify_raw and verify_raw.strip().lower() in ("0", "false", "no", "off"))

    return GrafanaConfig(
        url=(url or lookup("URL") or DEFAULT_URL).rstrip("/"),
        token=token or lookup("SERVICE_ACCOUNT_TOKEN", "TOKEN", "PAT", "API_KEY"),
        username=username,
        password=password,
        org_id=str(org_id) if org_id not in (None, "") else lookup("ORG_ID"),
        ca_bundle=lookup("CA_BUNDLE"),
        tls_verify=tls_verify,
        proxy=lookup("PROXY"),
        env=env,
    )


def _client_kwargs(config: GrafanaConfig) -> dict[str, Any]:
    headers = {"User-Agent": "runspec-grafana-core", "Accept": "application/json", **config.headers}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"
    if config.org_id:
        headers["X-Grafana-Org-Id"] = str(config.org_id)
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


def grafana_request(
    method: str,
    path: str,
    *,
    config: GrafanaConfig,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
) -> Any:
    """Make a request against ``{config.url}{path}`` and return the decoded JSON
    body (``None`` for an empty body, the text when the body isn't JSON).

    ``None``/empty params are dropped; list values repeat the key (``tag``).
    Raises :class:`GrafanaError` on an HTTP error, carrying Grafana's own
    ``message`` (or the upstream datasource's ``error``).
    """
    with httpx.Client(**_client_kwargs(config)) as client:
        try:
            resp = client.request(method, f"{config.url}{path}", params=_clean(params), json=json_body)
        except httpx.HTTPError as exc:
            raise GrafanaError(f"Cannot reach Grafana at {config.url}: {exc}") from exc
    if resp.status_code >= 400:
        raise _api_error(resp)
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _api_error(resp: httpx.Response) -> GrafanaError:
    try:
        body = resp.json()
    except ValueError:
        body = None
    message = ""
    if isinstance(body, dict):
        message = str(body.get("message") or body.get("error") or "")
    if not message:
        message = resp.text.strip()[:300] or resp.reason_phrase
    hint = ""
    if resp.status_code == 401:
        hint = " (check GRAFANA_SERVICE_ACCOUNT_TOKEN or GRAFANA_USERNAME/PASSWORD)"
    elif resp.status_code == 403:
        hint = " (the token's role lacks this permission: Viewer covers most reads; dashboard versions and creating annotations need Editor)"
    return GrafanaError(f"Grafana {resp.status_code}: {message}{hint}", resp.status_code)


# Codes on which the datasource proxy route is retried through the plugin's
# resources route — some managed Grafanas (Azure) answer only on /resources,
# and /resources doesn't always forward a datasource's basic auth (so the proxy
# route goes first). Same two routes the Grafana MCP server tries.
_FALLBACK_CODES = (401, 403, 404, 500, 502)


def datasource_request(config: GrafanaConfig, uid: str, path: str, params: dict[str, Any] | None = None) -> Any:
    """GET ``path`` on a datasource's own API through Grafana
    (``/api/datasources/proxy/uid/{uid}{path}``, falling back to
    ``/api/datasources/uid/{uid}/resources{path}``).

    Unwraps a Prometheus/Loki ``{"status": ..., "data": ...}`` envelope to
    ``{"data": ..., "warnings"?: [...]}`` and raises on ``"status": "error"``.
    """
    uid_part = quote(uid, safe="")
    try:
        body = grafana_request("GET", f"/api/datasources/proxy/uid/{uid_part}{path}", config=config, params=params)
    except GrafanaError as exc:
        if exc.status_code not in _FALLBACK_CODES:
            raise
        try:
            body = grafana_request("GET", f"/api/datasources/uid/{uid_part}/resources{path}", config=config, params=params)
        except GrafanaError:
            raise exc from None
    if isinstance(body, dict) and body.get("status") == "error":
        raise GrafanaError(f"Datasource {uid}: {body.get('error', 'unknown error')}")
    if isinstance(body, dict) and "status" in body and "data" in body:
        out = {"data": body["data"]}
        for key in ("warnings", "infos"):
            if body.get(key):
                out[key] = body[key]
        return out
    return {"data": body}


# ── time handling ──────────────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^-?((\d+)(ms|y|w|d|h|m|s))+$")
_DURATION_PART = re.compile(r"(\d+)(ms|y|w|d|h|m|s)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


def parse_duration(value: str) -> float:
    """Parse a duration (``30s``, ``5m``, ``1h30m``, ``2d``) to seconds.

    A leading ``-`` is ignored (callers mean "that long ago" either way).
    Raises :class:`GrafanaError` when it isn't a duration.
    """
    text = value.strip()
    if not _DURATION_RE.match(text):
        raise GrafanaError(f"Cannot parse {value!r} as a duration (e.g. 30s, 5m, 1h30m, 2d)")
    return float(sum(int(n) * _UNIT_SECONDS[unit] for n, unit in _DURATION_PART.findall(text)))


def parse_time(value: str | float | int | None, *, now: float | None = None) -> float | None:
    """Parse a timestamp to Unix seconds, or ``None`` when ``value`` is empty.

    Accepts Unix seconds (``1733000000``) or milliseconds (13 digits), RFC 3339
    (``2026-10-12T09:00:00Z``), ``now``, Grafana's ``now-1h``, or a duration
    back from now (``5m``, ``1h30m``).
    """
    if value is None or value == "":
        return None
    current = time.time() if now is None else now
    if isinstance(value, (int, float)):
        return _seconds(float(value))
    text = value.strip()
    if text.lower() == "now":
        return current
    if text.lower().startswith("now-"):
        text = text[4:]
    try:
        return _seconds(float(text))
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
    except GrafanaError:
        raise GrafanaError(f"Cannot parse {value!r} as a time — use Unix seconds, RFC 3339, or a duration back from now (e.g. 1h, now-6h)") from None


def _seconds(value: float) -> float:
    # A 13-digit number is epoch milliseconds (what Grafana's own URLs carry).
    return value / 1000.0 if value > 1e11 else value


def window(start: str | float | None, end: str | float | None, *, default_lookback: float = 3600.0, now: float | None = None) -> tuple[float, float]:
    """Resolve a ``start``/``end`` pair to Unix seconds: ``end`` defaults to now,
    ``start`` to ``default_lookback`` seconds before ``end``."""
    current = time.time() if now is None else now
    end_ts = parse_time(end, now=current)
    if end_ts is None:
        end_ts = current
    start_ts = parse_time(start, now=current)
    if start_ts is None:
        start_ts = end_ts - default_lookback
    if start_ts >= end_ts:
        raise GrafanaError(f"start ({iso(start_ts)}) must be before end ({iso(end_ts)})")
    return start_ts, end_ts


def iso(ts: float | None) -> str | None:
    """Unix seconds → RFC 3339 UTC."""
    if ts is None:
        return None
    spec = "seconds" if float(ts).is_integer() else "milliseconds"
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec=spec).replace("+00:00", "Z")


def truncate(items: list[Any], limit: int | None) -> tuple[list[Any], bool]:
    """Cap ``items`` at ``limit`` entries; ``None``/``0``/negative means no cap."""
    if limit is None or limit <= 0 or len(items) <= limit:
        return items, False
    return items[:limit], True


def preview(text: str | None, max_chars: int | None) -> tuple[str | None, int | None]:
    """Cut ``text`` to ``max_chars``; returns ``(text, full_length)`` where
    ``full_length`` is ``None`` when nothing was cut. ``0``/``None`` = no cut."""
    if text is None or not max_chars or max_chars <= 0 or len(text) <= max_chars:
        return text, None
    return text[:max_chars].rstrip() + "…", len(text)


def drop_empty(record: dict[str, Any]) -> dict[str, Any]:
    """Leave out ``None``, empty strings, lists and dicts (counts of 0 stay)."""
    return {k: v for k, v in record.items() if v is not None and v != "" and v != [] and v != {}}

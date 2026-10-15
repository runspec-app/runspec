"""netproxy.py — one console-wide HTTP proxy setting for outbound HTTP.

The console makes outbound HTTP(S) requests from several places: the agent's
``fetch_url`` tool, the Marketplace index reads, the Phoenix eval export, the
sandbox's WASI-binary download, external MCP servers over http/sse, the
LangServe / Anthropic / Bedrock model adapters and the OTLP telemetry exporters.
``[http]`` in config.toml routes all of them the same way:

- ``proxy = ""`` (default) — **system**: each client keeps its own default,
  which honours the standard ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``NO_PROXY``
  environment variables (urllib also reads the Windows Internet Options proxy).
  A console with no ``[http]`` section behaves exactly as before.
- ``proxy = "http://proxy.corp:8080"`` (or ``proxy.corp:8080``) — **manual**:
  every request goes through that HTTP CONNECT proxy, except hosts listed in
  ``no_proxy`` and loopback addresses (a local MCP server or Phoenix on
  ``127.0.0.1`` is never sent to a corporate proxy).
- ``proxy = "direct"`` — **direct**: never use a proxy, even when the
  environment sets one.

``credential`` names a Credentials-tab entry (``userpass``) whose username and
secret authenticate to the proxy (HTTP Basic); only the id is stored.

Pure stdlib. The bridge resolves the credential and calls :func:`configure` at
bring-up and on save; :func:`current` falls back to the saved config (without a
credential) when nothing configured it yet, so a code path that runs before
bring-up still gets the proxy.
"""

from __future__ import annotations

import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

DIRECT = "direct"

# Loopback is never proxied in manual mode: a corporate proxy can't reach the
# operator's own machine, and local MCP servers / Phoenix live there.
_ALWAYS_BYPASS = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class ProxySettings:
    """The resolved ``[http]`` proxy. ``proxy`` is ``""`` (system), ``"direct"``
    or a normalised proxy URL (credentials, when set, embedded as userinfo)."""

    proxy: str = ""
    no_proxy: tuple[str, ...] = ()

    @property
    def mode(self) -> str:
        if not self.proxy:
            return "system"
        if self.proxy == DIRECT:
            return "direct"
        return "manual"


def normalize_proxy_url(value: Any) -> str:
    """``""`` / ``"direct"`` pass through; ``host:port`` gains ``http://``."""
    p = str(value or "").strip()
    if not p:
        return ""
    if p.lower() == DIRECT:
        return DIRECT
    return p if "://" in p else f"http://{p}"


def split_no_proxy(value: Any) -> list[str]:
    """``no_proxy`` as a list — accepts a TOML array or a comma/space string."""
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = str(value or "").replace(",", " ").split()
    return [i.strip() for i in items if i.strip()]


def validate_proxy_url(value: Any) -> str | None:
    """An error message for a bad ``proxy`` value, or ``None`` when it's fine."""
    p = normalize_proxy_url(value)
    if p in ("", DIRECT):
        return None
    parts = urllib.parse.urlsplit(p)
    if parts.scheme not in ("http", "https"):
        return f"proxy must be an http:// or https:// URL (got {parts.scheme}://)"
    if not parts.hostname:
        return "proxy must name a host, e.g. http://proxy.corp:8080"
    try:
        parts.port  # noqa: B018 — raises ValueError on a bad port
    except ValueError:
        return "proxy has an invalid port"
    return None


def with_auth(url: str, username: str, password: str) -> str:
    """``url`` with ``username:password`` set as (URL-quoted) userinfo, replacing
    any userinfo it already carried. No-op without a username."""
    if not username or url in ("", DIRECT):
        return url
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    userinfo = urllib.parse.quote(username, safe="")
    if password:
        userinfo += ":" + urllib.parse.quote(password, safe="")
    return urllib.parse.urlunsplit(
        (parts.scheme, f"{userinfo}@{host}", parts.path, parts.query, parts.fragment)
    )


def redact(url: str) -> str:
    """``url`` with any password in its userinfo replaced, for logs and the UI."""
    if url in ("", DIRECT) or "@" not in url:
        return url
    parts = urllib.parse.urlsplit(url)
    if parts.password is None:
        return url
    netloc = parts.netloc.rsplit("@", 1)
    user = netloc[0].split(":", 1)[0]
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            f"{user}:***@{netloc[1]}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


def from_config(
    raw: dict[str, Any] | None, auth: tuple[str, str] | None = None
) -> ProxySettings:
    """Build settings from an ``[http]`` table (``config.read_http()``) and an
    optional resolved ``(username, password)`` for the proxy."""
    cfg = raw or {}
    proxy = normalize_proxy_url(cfg.get("proxy"))
    if validate_proxy_url(proxy):
        proxy = ""  # a malformed value never breaks requests — fall back to system
    if auth and proxy not in ("", DIRECT):
        proxy = with_auth(proxy, auth[0], auth[1])
    return ProxySettings(
        proxy=proxy, no_proxy=tuple(split_no_proxy(cfg.get("no_proxy")))
    )


# ── process-wide current settings ──────────────────────────────────────────

_lock = threading.Lock()
_current: ProxySettings | None = None


def configure(settings: ProxySettings | None) -> None:
    """Set the console-wide proxy (``None`` re-reads the saved config lazily)."""
    global _current
    with _lock:
        _current = settings


def current() -> ProxySettings:
    """The configured settings, else the saved ``[http]`` config (no credential)."""
    with _lock:
        if _current is not None:
            return _current
    try:
        from . import config as config_mod

        return from_config(config_mod.read_http())
    except Exception:  # noqa: BLE001 — a config problem must never break HTTP
        return ProxySettings()


# ── routing ────────────────────────────────────────────────────────────────


def _host_of(url_or_host: str) -> str:
    s = str(url_or_host or "")
    if "://" in s:
        return urllib.parse.urlsplit(s).hostname or ""
    return s


def bypassed(url_or_host: str, settings: ProxySettings | None = None) -> bool:
    """True when ``url_or_host`` skips the manual proxy (``no_proxy`` match or
    loopback). ``no_proxy`` follows the usual ``NO_PROXY`` rules: ``*`` matches
    everything, ``corp.local`` / ``.corp.local`` match the domain and its
    subdomains, ``host:port`` matches only that port."""
    s = settings or current()
    host = _host_of(url_or_host)
    if not host:
        return False
    bare = host.strip("[]").lower()
    if bare in _ALWAYS_BYPASS or bare.startswith("127."):
        return True
    if not s.no_proxy:
        return False
    return bool(
        urllib.request.proxy_bypass_environment(host, {"no": ",".join(s.no_proxy)})
    )


def proxy_for(url: str, settings: ProxySettings | None = None) -> str | None:
    """The route for ``url``: ``None`` = the client's own default (system mode),
    ``""`` = no proxy, otherwise the proxy URL to use."""
    s = settings or current()
    if s.mode == "system":
        return None
    if s.mode == "direct" or bypassed(url, s):
        return ""
    return s.proxy


class _ManualProxyHandler(urllib.request.ProxyHandler):
    """A ProxyHandler for one explicit proxy that also honours ``no_proxy``."""

    def __init__(self, settings: ProxySettings) -> None:
        super().__init__({"http": settings.proxy, "https": settings.proxy})
        self._settings = settings

    def proxy_open(self, req: Any, proxy: str, type: str) -> Any:  # noqa: A002
        if bypassed(req.host, self._settings):
            return None  # let the next handler open it directly
        return super().proxy_open(req, proxy, type)


def urllib_handler(settings: ProxySettings | None = None) -> urllib.request.BaseHandler:
    """The urllib ProxyHandler for the settings: environment / system proxies in
    system mode, none in direct mode, the explicit proxy in manual mode."""
    s = settings or current()
    if s.mode == "direct":
        return urllib.request.ProxyHandler({})
    if s.mode == "manual":
        return _ManualProxyHandler(s)
    return urllib.request.ProxyHandler()


def httpx_kwargs(url: str, settings: ProxySettings | None = None) -> dict[str, Any]:
    """Extra ``httpx.Client`` / ``AsyncClient`` kwargs for a client that talks
    to ``url``: ``{}`` in system mode (httpx reads the env itself),
    ``{"trust_env": False}`` for no proxy, ``{"proxy": url}`` otherwise."""
    route = proxy_for(url, settings)
    if route is None:
        return {}
    if route == "":
        return {"trust_env": False}
    return {"proxy": route}


def env_vars(settings: ProxySettings | None = None) -> dict[str, str]:
    """The standard ``*_PROXY`` variables for a subprocess (manual mode only)."""
    s = settings or current()
    if s.mode != "manual":
        return {}
    no = ",".join([*_ALWAYS_BYPASS, *s.no_proxy])
    return {
        "HTTPS_PROXY": s.proxy,
        "HTTP_PROXY": s.proxy,
        "https_proxy": s.proxy,
        "http_proxy": s.proxy,
        "NO_PROXY": no,
        "no_proxy": no,
    }


def describe(settings: ProxySettings | None = None) -> str:
    """A one-line, password-free summary for status lines and logs."""
    s = settings or current()
    if s.mode == "system":
        return "system (HTTPS_PROXY / HTTP_PROXY / NO_PROXY)"
    if s.mode == "direct":
        return "direct (no proxy)"
    extra = f", bypass {', '.join(s.no_proxy)}" if s.no_proxy else ""
    return f"{redact(s.proxy)}{extra}"

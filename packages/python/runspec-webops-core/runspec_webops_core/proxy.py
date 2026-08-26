"""Proxy discovery + CONNECT tunnelling — stdlib only (``socket`` / ``ctypes``).

The web/TLS helpers in this package open connections **directly** to the target
host. On a corporate network where internal hosts are only reachable through an
HTTP proxy, a direct connect just times out — which is why ``http-check`` /
``api-spec`` / ``cert-check`` hang against internal addresses while Edge/Chrome
work.

This module supplies the missing layer: it resolves *which* proxy (if any)
applies to a given URL, mirroring how the browsers decide, and it opens
connections *through* that proxy (HTTP ``CONNECT`` tunnelling for TLS/HTTPS, the
absolute-URI request form for plain HTTP).

Resolution order for a target URL (:func:`resolve_proxy`):

1. an **explicit** override (the runnable's ``--proxy`` arg / ``proxy=`` param);
   the literal ``"direct"`` forces a direct connection;
2. the **environment** — ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY`` with
   ``NO_PROXY`` honoured (this is what makes the runnables work automatically
   wherever those are already set, cross-platform);
3. on **Windows**, the *system* configuration exactly as the browsers see it,
   via WinHTTP — **WPAD auto-detect and PAC scripts evaluated per-URL**
   (``WinHttpGetProxyForUrl``), then the per-user manual proxy, then the
   machine-wide ``netsh winhttp`` default;
4. otherwise a **direct** connection.

Everything is best-effort: any failure in the Windows path falls through to a
direct connection rather than raising, so discovery can never break a probe that
would otherwise have worked.
"""

from __future__ import annotations

import base64
import os
import re
import socket
import sys
from urllib.parse import urlsplit

from runspec_webops_core.errors import ConnectError

# Tokens (case-insensitive) that force a direct connection when passed explicitly.
_DIRECT_TOKENS = {"direct", "none", "off", "no", "false"}

_CONNECT_MAX_HEADER_BYTES = 65_536


# ── pure parsing / matching ─────────────────────────────────────────────────────


def normalize_proxy(value: str | None) -> str | None:
    """Reduce a proxy reference to a bare ``[user:pass@]host:port``.

    Strips any ``scheme://`` prefix and trailing path, so ``http://p:8080/`` and
    ``p:8080`` normalise identically. Returns ``None`` for an empty value.
    """
    if not value:
        return None
    token = value.strip()
    if "://" in token:
        token = token.split("://", 1)[1]
    token = token.split("/", 1)[0]  # drop any path
    return token or None


def _first_token(server: str) -> str | None:
    """First proxy in a space/semicolon-separated list (PAC / WinHTTP form)."""
    for part in re.split(r"[;\s]+", server.strip()):
        if part:
            return part
    return None


def pick_scheme_proxy(server: str | None, scheme: str) -> str | None:
    """Pick the proxy for ``scheme`` out of a WinINET/env ``ProxyServer`` string.

    Handles both the bare form (``10.0.0.1:8080`` — applies to every scheme) and
    the per-scheme form (``http=10.0.0.1:8080;https=10.0.0.1:8443``). Falls back
    to the ``http`` entry, then to any entry.
    """
    if not server:
        return None
    server = server.strip()
    if "=" not in server:
        return normalize_proxy(_first_token(server))
    mapping: dict[str, str] = {}
    for part in re.split(r"[;\s]+", server):
        if "=" in part:
            key, val = part.split("=", 1)
            key, val = key.strip().lower(), val.strip()
            if key and val:
                mapping[key] = val
    chosen = mapping.get(scheme.lower()) or mapping.get("http") or next(iter(mapping.values()), None)
    return normalize_proxy(chosen)


def split_no_proxy(value: str | None) -> list[str]:
    """Split a ``NO_PROXY`` / WinINET ``ProxyOverride`` list into entries."""
    if not value:
        return []
    return [e.strip() for e in re.split(r"[;,\s]+", value) if e.strip()]


def host_bypasses(host: str, entries: list[str]) -> bool:
    """Whether ``host`` matches a proxy-bypass list (``NO_PROXY`` / override).

    Recognises ``*`` (bypass everything), the WinINET ``<local>`` token (bypass
    dotless/simple hostnames), exact matches, and domain-suffix matches
    (``.corp`` / ``corp`` / ``*.corp`` all match ``api.corp``).
    """
    if not host:
        return False
    host = host.lower().rstrip(".")
    for raw in entries:
        entry = raw.lower().strip()
        if not entry:
            continue
        if entry == "*":
            return True
        if entry == "<local>":
            if "." not in host:
                return True
            continue
        entry = entry.lstrip("*")  # "*.corp" -> ".corp"
        suffix = entry.lstrip(".")
        if not suffix:
            continue
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def split_proxy(proxy: str) -> tuple[str, int, dict[str, str]]:
    """Split a normalised proxy into ``(host, port, connect_headers)``.

    Embedded ``user:pass@`` credentials become a ``Proxy-Authorization: Basic``
    header (used on the ``CONNECT`` request and via ``set_tunnel``). The default
    port is 8080 when none is given.
    """
    normalized = normalize_proxy(proxy)
    if not normalized:
        raise ConnectError(f"invalid proxy: {proxy!r}")
    creds: str | None = None
    if "@" in normalized:
        creds, normalized = normalized.rsplit("@", 1)
    host, _, port_s = normalized.partition(":")
    try:
        port = int(port_s) if port_s else 8080
    except ValueError as e:
        raise ConnectError(f"invalid proxy port in {proxy!r}: {port_s!r}") from e
    if not host:
        raise ConnectError(f"invalid proxy host in {proxy!r}")
    headers: dict[str, str] = {}
    if creds:
        token = base64.b64encode(creds.encode("utf-8")).decode("ascii")
        headers["Proxy-Authorization"] = f"Basic {token}"
    return host, port, headers


# ── environment tier ────────────────────────────────────────────────────────────


def _env(name: str) -> str | None:
    return os.environ.get(name.upper()) or os.environ.get(name.lower())


def _env_proxy_for(scheme: str) -> tuple[str | None, list[str]]:
    """The env proxy for ``scheme`` (if any) plus the parsed ``NO_PROXY`` list."""
    no_proxy = split_no_proxy(_env("NO_PROXY"))
    scheme_var = "HTTPS_PROXY" if scheme == "https" else "HTTP_PROXY"
    proxy = _env(scheme_var) or _env("ALL_PROXY")
    return proxy, no_proxy


# ── Windows system tier (WinHTTP: WPAD + PAC + manual + machine default) ─────────


def _windows_proxy_for_url(url: str) -> str | None:  # pragma: no cover - Windows only
    """Resolve the proxy for ``url`` from the Windows system config via WinHTTP.

    Evaluates, in order, exactly what the browsers do: WPAD auto-detect and any
    per-user PAC script (``WinHttpGetProxyForUrl``), the per-user manual proxy
    (with its bypass list), and finally the machine-wide ``netsh winhttp``
    default. Returns a ``host:port`` string, or ``None`` for a direct connection.
    Any failure is swallowed by the caller.
    """
    import ctypes
    from ctypes import wintypes

    winhttp = ctypes.WinDLL("winhttp", use_last_error=True)  # type: ignore[attr-defined]  # Windows only
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]  # Windows only

    class IEProxyConfig(ctypes.Structure):
        _fields_ = [
            ("fAutoDetect", wintypes.BOOL),
            ("lpszAutoConfigUrl", ctypes.c_void_p),
            ("lpszProxy", ctypes.c_void_p),
            ("lpszProxyBypass", ctypes.c_void_p),
        ]

    class AutoProxyOptions(ctypes.Structure):
        _fields_ = [
            ("dwFlags", wintypes.DWORD),
            ("dwAutoDetectFlags", wintypes.DWORD),
            ("lpszAutoConfigUrl", wintypes.LPCWSTR),
            ("lpvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("fAutoLogonIfChallenged", wintypes.BOOL),
        ]

    class ProxyInfo(ctypes.Structure):
        _fields_ = [
            ("dwAccessType", wintypes.DWORD),
            ("lpszProxy", ctypes.c_void_p),
            ("lpszProxyBypass", ctypes.c_void_p),
        ]

    WINHTTP_AUTOPROXY_AUTO_DETECT = 0x00000001
    WINHTTP_AUTOPROXY_CONFIG_URL = 0x00000002
    WINHTTP_AUTO_DETECT_TYPE_DHCP = 0x00000001
    WINHTTP_AUTO_DETECT_TYPE_DNS_A = 0x00000002
    WINHTTP_ACCESS_TYPE_NO_PROXY = 1

    winhttp.WinHttpGetIEProxyConfigForCurrentUser.argtypes = [ctypes.POINTER(IEProxyConfig)]
    winhttp.WinHttpGetIEProxyConfigForCurrentUser.restype = wintypes.BOOL
    winhttp.WinHttpGetDefaultProxyConfiguration.argtypes = [ctypes.POINTER(ProxyInfo)]
    winhttp.WinHttpGetDefaultProxyConfiguration.restype = wintypes.BOOL
    winhttp.WinHttpOpen.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    winhttp.WinHttpOpen.restype = ctypes.c_void_p
    winhttp.WinHttpGetProxyForUrl.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(AutoProxyOptions), ctypes.POINTER(ProxyInfo)]
    winhttp.WinHttpGetProxyForUrl.restype = wintypes.BOOL
    winhttp.WinHttpCloseHandle.argtypes = [ctypes.c_void_p]
    winhttp.WinHttpCloseHandle.restype = wintypes.BOOL

    def read_free(ptr: int | None) -> str | None:
        if not ptr:
            return None
        text = ctypes.wstring_at(ptr)
        kernel32.GlobalFree(ctypes.c_void_p(ptr))
        return text or None

    scheme = urlsplit(url).scheme or "http"
    host = urlsplit(url).hostname or ""

    # Per-user IE/Edge config: auto-detect flag, PAC URL, manual proxy + bypass.
    auto_detect = False
    pac_url: str | None = None
    manual_proxy: str | None = None
    manual_bypass: str | None = None
    ie = IEProxyConfig()
    if winhttp.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(ie)):
        auto_detect = bool(ie.fAutoDetect)
        pac_url = read_free(ie.lpszAutoConfigUrl)
        manual_proxy = read_free(ie.lpszProxy)
        manual_bypass = read_free(ie.lpszProxyBypass)
    else:
        # No per-user config (e.g. a service / SYSTEM context): still try WPAD.
        auto_detect = True

    # 1. WPAD auto-detect and/or PAC — evaluated for this specific URL.
    if auto_detect or pac_url:
        session = winhttp.WinHttpOpen("runspec-webops", WINHTTP_ACCESS_TYPE_NO_PROXY, None, None, 0)
        if session:
            try:
                opts = AutoProxyOptions()
                opts.fAutoLogonIfChallenged = True
                if auto_detect:
                    opts.dwFlags |= WINHTTP_AUTOPROXY_AUTO_DETECT
                    opts.dwAutoDetectFlags = WINHTTP_AUTO_DETECT_TYPE_DHCP | WINHTTP_AUTO_DETECT_TYPE_DNS_A
                if pac_url:
                    opts.dwFlags |= WINHTTP_AUTOPROXY_CONFIG_URL
                    opts.lpszAutoConfigUrl = pac_url
                info = ProxyInfo()
                if winhttp.WinHttpGetProxyForUrl(session, url, ctypes.byref(opts), ctypes.byref(info)):
                    proxy = read_free(info.lpszProxy)
                    read_free(info.lpszProxyBypass)
                    if info.dwAccessType == WINHTTP_ACCESS_TYPE_NO_PROXY or not proxy:
                        return None  # PAC said DIRECT
                    return normalize_proxy(_first_token(proxy))
                # Auto-config failed (WPAD not present / PAC unreachable): fall through.
            finally:
                winhttp.WinHttpCloseHandle(session)

    # 2. Per-user manual proxy, honouring its bypass list.
    if manual_proxy:
        if host_bypasses(host, split_no_proxy(manual_bypass)):
            return None
        return pick_scheme_proxy(manual_proxy, scheme)

    # 3. Machine-wide default (`netsh winhttp set proxy`), for service contexts.
    default = ProxyInfo()
    if winhttp.WinHttpGetDefaultProxyConfiguration(ctypes.byref(default)):
        proxy = read_free(default.lpszProxy)
        bypass = read_free(default.lpszProxyBypass)
        if proxy and not host_bypasses(host, split_no_proxy(bypass)):
            return pick_scheme_proxy(proxy, scheme)

    return None


# ── resolution ──────────────────────────────────────────────────────────────────


def resolve_proxy(url: str, explicit: str | None = None) -> str | None:
    """Resolve the proxy to use for ``url``, or ``None`` for a direct connection.

    ``explicit`` (a runnable's ``--proxy`` value) overrides everything; the
    literal ``"direct"`` forces no proxy. With ``explicit`` unset, the
    environment then the Windows system configuration are consulted (see the
    module docstring). Never raises — a broken Windows lookup yields ``None``.
    """
    if explicit is not None and str(explicit).strip():
        value = str(explicit).strip()
        if value.lower() in _DIRECT_TOKENS:
            return None
        return normalize_proxy(value)

    parts = urlsplit(url)
    scheme = parts.scheme or "http"
    host = parts.hostname or ""

    env_proxy, no_proxy = _env_proxy_for(scheme)
    if env_proxy:
        if host_bypasses(host, no_proxy):
            return None
        return normalize_proxy(env_proxy)

    if sys.platform == "win32":
        try:
            return _windows_proxy_for_url(url)
        except Exception:
            return None

    return None


# ── connection helpers ──────────────────────────────────────────────────────────


def _read_connect_response(sock: socket.socket) -> bytes:
    """Read a proxy ``CONNECT`` response up to the blank-line header terminator."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > _CONNECT_MAX_HEADER_BYTES:
            raise ConnectError("proxy CONNECT response exceeded header limit")
    return buf


def _connect_status(response: bytes) -> int:
    """Parse the HTTP status code out of a proxy ``CONNECT`` status line."""
    try:
        line = response.split(b"\r\n", 1)[0].decode("latin-1")
        return int(line.split(" ", 2)[1])
    except (IndexError, ValueError) as e:
        raise ConnectError(f"malformed proxy CONNECT response: {response[:80]!r}") from e


def open_socket(host: str, port: int, timeout: float, proxy: str | None = None) -> socket.socket:
    """Open a raw TCP socket to ``host:port``, tunnelling via ``proxy`` if given.

    With ``proxy`` set, connects to the proxy and issues an HTTP ``CONNECT`` so
    the caller can drive TLS to the real host through the tunnel (used by the TLS
    inspection code). Raises :class:`ConnectError` if the proxy refuses.
    """
    if not proxy:
        return socket.create_connection((host, port), timeout=timeout)

    phost, pport, pheaders = split_proxy(proxy)
    sock = socket.create_connection((phost, pport), timeout=timeout)
    try:
        sock.settimeout(timeout)
        lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
        lines += [f"{k}: {v}" for k, v in pheaders.items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        status = _connect_status(_read_connect_response(sock))
        if not (200 <= status < 300):
            raise ConnectError(f"proxy {phost}:{pport} refused CONNECT to {host}:{port}: HTTP {status}")
    except BaseException:
        sock.close()
        raise
    return sock


def http_connection(scheme: str, host: str, port: int, timeout: float, ssl_context, proxy: str | None):
    """Build an ``http.client`` connection, request-target prefix, and headers.

    Returns ``(conn, target_prefix, extra_headers)``. ``target_prefix`` is ``""``
    for a direct or CONNECT-tunnelled request (send the path), or
    ``"http://host[:port]"`` for a plain-HTTP request *through* a proxy, where the
    request line carries the absolute URI. ``extra_headers`` carries any
    ``Proxy-Authorization`` the caller must add to a plain-HTTP proxied request
    (for a tunnel it is passed via ``set_tunnel`` instead).
    """
    import http.client

    conn: http.client.HTTPConnection
    is_https = scheme == "https"
    if proxy:
        phost, pport, pheaders = split_proxy(proxy)
        if is_https:
            tunnel = http.client.HTTPSConnection(phost, pport, timeout=timeout, context=ssl_context)
            tunnel.set_tunnel(host, port, headers=pheaders or None)
            return tunnel, "", {}
        conn = http.client.HTTPConnection(phost, pport, timeout=timeout)
        netloc = host if port == 80 else f"{host}:{port}"
        return conn, f"http://{netloc}", dict(pheaders)

    if is_https:
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl_context), "", {}
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    return conn, "", {}

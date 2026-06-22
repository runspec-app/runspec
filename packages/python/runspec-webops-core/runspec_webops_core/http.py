"""HTTP(S) endpoint probing — stdlib ``http.client`` / ``ssl`` only.

A single request is issued and its status / timing / a few headers are captured;
the body is drained, not buffered. ``classify`` and the security-header analysis
are pure. Redirects are *not* followed by :func:`probe` — :func:`trace_redirects`
walks them explicitly so each hop is visible.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

from runspec_webops_core.errors import ConnectError

_USER_AGENT = "runspec-webops"

# Response security headers worth surfacing — present value or flagged as missing.
SECURITY_HEADERS: dict[str, str] = {
    "strict-transport-security": "strict_transport_security",
    "content-security-policy": "content_security_policy",
    "x-frame-options": "x_frame_options",
    "x-content-type-options": "x_content_type_options",
    "referrer-policy": "referrer_policy",
    "permissions-policy": "permissions_policy",
}


def classify(status: int) -> str:
    """Bucket an HTTP status code. Pure."""
    if 200 <= status < 300:
        return "ok"
    if 300 <= status < 400:
        return "redirect"
    if 400 <= status < 500:
        return "client-error"
    if 500 <= status < 600:
        return "server-error"
    return "unknown"


def _request(url: str, timeout: float, method: str, insecure: bool) -> tuple[int, str, list[tuple[str, str]], float]:
    """Issue one request and return ``(status, reason, headers, elapsed_ms)``.

    Raises :class:`ConnectError` on any transport-level failure.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ConnectError(f"unsupported URL scheme: {parts.scheme!r} (expected http or https)")
    if not parts.hostname:
        raise ConnectError(f"no host in URL: {url!r}")

    is_https = parts.scheme == "https"
    port = parts.port or (443 if is_https else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    if is_https:
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(parts.hostname, port, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)

    start = time.monotonic()
    try:
        conn.request(method, path, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"})
        resp = conn.getresponse()
        resp.read()  # drain so the socket can be reused/closed cleanly
        elapsed = round((time.monotonic() - start) * 1000, 1)
        return resp.status, resp.reason or "", resp.getheaders(), elapsed
    except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException, socket.gaierror) as e:
        raise ConnectError(f"request to {url} failed: {e}") from e
    finally:
        conn.close()


def fetch_body(url: str, timeout: float = 15.0, insecure: bool = False, max_bytes: int = 10_000_000, max_redirects: int = 5) -> tuple[int, str]:
    """GET a URL and return ``(status, body_text)``, following redirects.

    Used to retrieve documents (e.g. an OpenAPI spec). The body is size-capped so
    a runaway endpoint can't exhaust memory. Raises :class:`ConnectError` on a
    transport failure or when the body exceeds ``max_bytes``.
    """
    current = url
    seen: set[str] = set()
    for _ in range(max_redirects + 1):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https"):
            raise ConnectError(f"unsupported URL scheme: {parts.scheme!r} (expected http or https)")
        if not parts.hostname:
            raise ConnectError(f"no host in URL: {current!r}")
        if current in seen:
            raise ConnectError(f"redirect loop while fetching {url}")
        seen.add(current)

        is_https = parts.scheme == "https"
        port = parts.port or (443 if is_https else 80)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        if is_https:
            ctx = ssl.create_default_context()
            if insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(parts.hostname, port, timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)

        try:
            conn.request("GET", path, headers={"User-Agent": _USER_AGENT, "Accept": "application/json, application/yaml;q=0.9, */*;q=0.8"})
            resp = conn.getresponse()
            if 300 <= resp.status < 400:
                location = resp.getheader("location")
                resp.read()
                if location:
                    current = urljoin(current, location)
                    continue
            raw = resp.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ConnectError(f"response from {current} exceeds {max_bytes} bytes")
            charset = "utf-8"
            ctype = resp.getheader("content-type") or ""
            if "charset=" in ctype:
                charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
            return resp.status, raw.decode(charset, errors="replace")
        except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException, socket.gaierror) as e:
            raise ConnectError(f"request to {current} failed: {e}") from e
        finally:
            conn.close()

    raise ConnectError(f"too many redirects while fetching {url}")


def _header(headers: list[tuple[str, str]], name: str) -> str | None:
    target = name.lower()
    for key, value in headers:
        if key.lower() == target:
            return value
    return None


def probe(url: str, timeout: float = 10.0, method: str = "GET", insecure: bool = False) -> dict:
    """Single-request probe: status, timing, ``server`` / ``location`` headers."""
    status, reason, headers, elapsed = _request(url, timeout, method, insecure)
    return {
        "url": url,
        "status": status,
        "status_text": reason,
        "time_ms": elapsed,
        "server": _header(headers, "server"),
        "location": _header(headers, "location"),
        "class": classify(status),
    }


def fetch_headers(url: str, timeout: float = 10.0, method: str = "HEAD", insecure: bool = False) -> dict:
    """Fetch response headers and report on the security headers (present/missing)."""
    status, reason, headers, elapsed = _request(url, timeout, method, insecure)
    header_map = {key.lower(): value for key, value in headers}
    security: dict[str, str | None] = {}
    missing: list[str] = []
    for raw, friendly in SECURITY_HEADERS.items():
        value = header_map.get(raw)
        security[friendly] = value
        if value is None:
            missing.append(raw)
    return {
        "url": url,
        "status": status,
        "status_text": reason,
        "time_ms": elapsed,
        "class": classify(status),
        "headers": dict(header_map),
        "security": security,
        "missing_security_headers": missing,
    }


def trace_redirects(url: str, timeout: float = 10.0, max_hops: int = 10, insecure: bool = False) -> dict:
    """Walk the redirect chain, recording each hop until a non-3xx or ``max_hops``."""
    hops: list[dict] = []
    seen: set[str] = set()
    current = url
    final_status = 0
    for _ in range(max_hops + 1):
        if current in seen:
            hops.append({"url": current, "status": None, "status_text": "redirect loop", "location": None, "time_ms": 0.0})
            break
        seen.add(current)
        status, reason, headers, elapsed = _request(current, timeout, "GET", insecure)
        location = _header(headers, "location")
        hops.append(
            {
                "url": current,
                "status": status,
                "status_text": reason,
                "location": location,
                "time_ms": elapsed,
            }
        )
        final_status = status
        if not (300 <= status < 400) or not location:
            break
        current = urljoin(current, location)
    else:
        # Loop exhausted without a terminal response.
        hops[-1]["status_text"] = (hops[-1]["status_text"] or "") + " (max hops reached)"

    return {
        "url": url,
        "hops": hops,
        "final_url": hops[-1]["url"] if hops else url,
        "final_status": final_status,
        "redirect_count": max(len(hops) - 1, 0),
    }

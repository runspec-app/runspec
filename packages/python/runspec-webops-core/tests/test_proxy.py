"""Proxy discovery + tunnelling — pure resolution plus real loopback proxies."""

from __future__ import annotations

import contextlib
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from runspec_webops_core import open_socket, probe, resolve_proxy
from runspec_webops_core.errors import ConnectError
from runspec_webops_core.proxy import (
    host_bypasses,
    normalize_proxy,
    pick_scheme_proxy,
    split_no_proxy,
    split_proxy,
)

# ── pure parsing / matching ─────────────────────────────────────────────────────


def test_normalize_proxy_strips_scheme_and_path() -> None:
    assert normalize_proxy("http://p:8080") == "p:8080"
    assert normalize_proxy("p:8080") == "p:8080"
    assert normalize_proxy("http://p:8080/pac") == "p:8080"
    assert normalize_proxy("  https://p:3128/  ") == "p:3128"
    assert normalize_proxy("") is None
    assert normalize_proxy(None) is None


def test_pick_scheme_proxy_bare_and_per_scheme() -> None:
    assert pick_scheme_proxy("10.0.0.1:8080", "https") == "10.0.0.1:8080"
    assert pick_scheme_proxy("http=a:8080;https=b:8443", "https") == "b:8443"
    assert pick_scheme_proxy("http=a:8080;https=b:8443", "http") == "a:8080"
    # No matching scheme, no http entry -> first available.
    assert pick_scheme_proxy("ftp=c:2121", "https") == "c:2121"
    # PAC/WinHTTP list form (space/semicolon separated) -> first.
    assert pick_scheme_proxy("p1:8080 p2:8080", "https") == "p1:8080"
    assert pick_scheme_proxy(None, "https") is None


def test_split_no_proxy_variants() -> None:
    assert split_no_proxy("a.com, .b.com; c.com  d.com") == ["a.com", ".b.com", "c.com", "d.com"]
    assert split_no_proxy("") == []
    assert split_no_proxy(None) == []


def test_host_bypasses() -> None:
    assert host_bypasses("api.corp", ["*"]) is True
    assert host_bypasses("api.corp", [".corp"]) is True
    assert host_bypasses("api.corp", ["corp"]) is True
    assert host_bypasses("api.corp", ["*.corp"]) is True
    assert host_bypasses("corp", ["corp"]) is True
    assert host_bypasses("api.corp", ["other.com"]) is False
    assert host_bypasses("internalhost", ["<local>"]) is True  # dotless
    assert host_bypasses("api.corp", ["<local>"]) is False  # has a dot
    assert host_bypasses("", ["*"]) is False


def test_split_proxy_host_port_and_auth() -> None:
    host, port, headers = split_proxy("p:8080")
    assert (host, port) == ("p", 8080)
    assert headers == {}
    host, port, headers = split_proxy("http://user:pass@p:3128")
    assert (host, port) == ("p", 3128)
    assert headers["Proxy-Authorization"].startswith("Basic ")
    # Default port when omitted.
    assert split_proxy("p")[1] == 8080
    with pytest.raises(ConnectError):
        split_proxy("p:notaport")


# ── resolution ──────────────────────────────────────────────────────────────────


def test_resolve_explicit_override_and_direct() -> None:
    assert resolve_proxy("https://x.com", "10.0.0.1:8080") == "10.0.0.1:8080"
    assert resolve_proxy("https://x.com", "http://10.0.0.1:8080") == "10.0.0.1:8080"
    assert resolve_proxy("https://x.com", "direct") is None
    assert resolve_proxy("https://x.com", "DIRECT") is None


def test_resolve_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://envproxy:8080")
    assert resolve_proxy("https://api.example.com/x") == "envproxy:8080"
    # http scheme has no HTTP_PROXY set -> direct.
    assert resolve_proxy("http://api.example.com/x") is None


def test_resolve_env_no_proxy_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.lower(), raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "envproxy:8080")
    monkeypatch.setenv("NO_PROXY", ".internal")
    assert resolve_proxy("https://svc.internal/x") is None  # bypassed
    assert resolve_proxy("https://svc.public.com/x") == "envproxy:8080"


def test_explicit_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "envproxy:8080")
    assert resolve_proxy("https://x.com", "override:3128") == "override:3128"
    assert resolve_proxy("https://x.com", "direct") is None


# ── loopback proxies (real I/O) ─────────────────────────────────────────────────


class _AbsoluteUriProxy(BaseHTTPRequestHandler):
    """A minimal forward proxy: a plain-HTTP request carries an absolute URI."""

    seen_targets: list[str] = []

    def log_message(self, *_a) -> None:  # silence
        pass

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen_targets.append(self.path)
        body = b"proxied-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(server: HTTPServer) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


def test_probe_through_http_proxy_uses_absolute_uri() -> None:
    _AbsoluteUriProxy.seen_targets = []
    proxy = HTTPServer(("127.0.0.1", 0), _AbsoluteUriProxy)
    _serve(proxy)
    try:
        paddr = f"127.0.0.1:{proxy.server_address[1]}"
        result = probe("http://target.internal/health", timeout=5.0, proxy=paddr)
    finally:
        proxy.shutdown()
    assert result["status"] == 200
    # The proxy received the full absolute URI, not just the path.
    assert _AbsoluteUriProxy.seen_targets == ["http://target.internal/health"]


class _ConnectProxy(threading.Thread):
    """A one-shot CONNECT tunnelling proxy bridging to an arbitrary target."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.saw_connect: str | None = None

    def run(self) -> None:
        client, _ = self.listener.accept()
        with client:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = client.recv(4096)
                if not chunk:
                    return
                req += chunk
            first = req.split(b"\r\n", 1)[0].decode()
            self.saw_connect = first
            _, hostport, _ = first.split(" ", 2)
            host, port = hostport.rsplit(":", 1)
            upstream = socket.create_connection((host, int(port)))
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self._bridge(client, upstream)

    @staticmethod
    def _bridge(a: socket.socket, b: socket.socket) -> None:
        def pipe(src: socket.socket, dst: socket.socket) -> None:
            try:
                while True:
                    data = src.recv(4096)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    dst.shutdown(socket.SHUT_WR)

        t = threading.Thread(target=pipe, args=(a, b), daemon=True)
        t.start()
        pipe(b, a)
        t.join(timeout=2)


class _Target(BaseHTTPRequestHandler):
    def log_message(self, *_a) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_open_socket_connect_tunnel() -> None:
    target = HTTPServer(("127.0.0.1", 0), _Target)
    _serve(target)
    proxy = _ConnectProxy()
    proxy.start()
    thost, tport = "127.0.0.1", target.server_address[1]
    try:
        sock = open_socket(thost, tport, timeout=5.0, proxy=f"127.0.0.1:{proxy.port}")
        with sock:
            sock.sendall(f"GET /x HTTP/1.1\r\nHost: {thost}:{tport}\r\nConnection: close\r\n\r\n".encode())
            data = sock.recv(1024)
    finally:
        target.shutdown()
    assert b"204" in data
    assert proxy.saw_connect == f"CONNECT {thost}:{tport} HTTP/1.1"


def test_open_socket_direct_still_works() -> None:
    target = HTTPServer(("127.0.0.1", 0), _Target)
    _serve(target)
    thost, tport = "127.0.0.1", target.server_address[1]
    try:
        sock = open_socket(thost, tport, timeout=5.0)  # no proxy
        with sock:
            sock.sendall(f"GET /x HTTP/1.1\r\nHost: {thost}\r\nConnection: close\r\n\r\n".encode())
            data = sock.recv(1024)
    finally:
        target.shutdown()
    assert b"204" in data


def test_open_socket_bad_proxy_raises() -> None:
    # Connecting to a closed port for the proxy should surface as a failure.
    with pytest.raises((ConnectError, OSError)):
        open_socket("example.com", 443, timeout=1.0, proxy="127.0.0.1:1")

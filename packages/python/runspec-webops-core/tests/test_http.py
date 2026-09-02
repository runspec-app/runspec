"""HTTP helpers — pure classification plus a real loopback server for I/O."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from runspec_webops_core import classify, fetch_headers, probe, trace_redirects
from runspec_webops_core.errors import ConnectError


def test_classify_buckets() -> None:
    assert classify(204) == "ok"
    assert classify(301) == "redirect"
    assert classify(404) == "client-error"
    assert classify(503) == "server-error"
    assert classify(100) == "unknown"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # silence test server
        pass

    def _send(self, code: int, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler dispatch name
        self._route()

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler dispatch name
        self._route()

    def _route(self) -> None:
        if self.path == "/ok":
            self._send(200, {"Server": "test", "Strict-Transport-Security": "max-age=63072000"})
        elif self.path == "/one":
            self._send(302, {"Location": "/two"})
        elif self.path == "/two":
            self._send(302, {"Location": "/ok"})
        elif self.path == "/loop":
            self._send(302, {"Location": "/loop"})
        else:
            self._send(404)


@pytest.fixture()
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    yield f"http://{host}:{port}"
    httpd.shutdown()


def test_probe_reports_status_and_class(server: str) -> None:
    res = probe(f"{server}/ok", timeout=5)
    assert res["status"] == 200
    assert res["class"] == "ok"
    assert res["server"] is not None  # BaseHTTPServer sets its own Server header
    assert res["location"] is None
    assert isinstance(res["time_ms"], float)


def test_probe_does_not_follow_redirect(server: str) -> None:
    res = probe(f"{server}/one", timeout=5)
    assert res["status"] == 302
    assert res["class"] == "redirect"
    assert res["location"] == "/two"


def test_probe_bad_scheme_raises() -> None:
    with pytest.raises(ConnectError):
        probe("ftp://example.com", timeout=2)


def test_probe_bad_ca_bundle_raises() -> None:
    # A missing CA bundle fails at context build, before any network I/O.
    with pytest.raises(ConnectError, match="CA bundle"):
        probe("https://example.com", timeout=2, ca_bundle="/no/such/ca.pem")


def test_fetch_headers_flags_missing_security(server: str) -> None:
    res = fetch_headers(f"{server}/ok", timeout=5)
    assert res["security"]["strict_transport_security"] == "max-age=63072000"
    assert res["security"]["content_security_policy"] is None
    assert "content-security-policy" in res["missing_security_headers"]
    assert "strict-transport-security" not in res["missing_security_headers"]


def test_trace_redirects_walks_chain(server: str) -> None:
    res = trace_redirects(f"{server}/one", timeout=5)
    assert [hop["status"] for hop in res["hops"]] == [302, 302, 200]
    assert res["final_status"] == 200
    assert res["redirect_count"] == 2
    assert res["final_url"].endswith("/ok")


def test_trace_redirects_detects_loop(server: str) -> None:
    res = trace_redirects(f"{server}/loop", timeout=5, max_hops=5)
    # Terminates on the repeated URL rather than spinning to max_hops.
    assert res["hops"][-1]["status_text"] == "redirect loop"

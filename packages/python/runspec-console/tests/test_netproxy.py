"""The console-wide ``[http]`` proxy — :mod:`runspec_console.netproxy`, its
config reader, the Network-tab bridge methods, and the clients that follow it
(``fetch_url`` over urllib, httpx kwargs for MCP / the model adapters)."""

from __future__ import annotations

import base64
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from runspec_console import config as config_mod
from runspec_console import netproxy, webfetch
from runspec_console.bridge_network import NetworkMixin

MANUAL = netproxy.ProxySettings(
    proxy="http://proxy.corp:8080", no_proxy=("corp.local",)
)


@pytest.fixture(autouse=True)
def _reset_current():
    netproxy.configure(None)
    yield
    netproxy.configure(None)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    return tmp_path


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_normalize_and_validate():
    assert netproxy.normalize_proxy_url("") == ""
    assert netproxy.normalize_proxy_url(" DIRECT ") == "direct"
    assert netproxy.normalize_proxy_url("proxy:3128") == "http://proxy:3128"
    assert netproxy.validate_proxy_url("proxy:3128") is None
    assert netproxy.validate_proxy_url("direct") is None
    assert "http://" in netproxy.validate_proxy_url("socks5://p:1080")
    assert "port" in netproxy.validate_proxy_url("http://p:notaport")


def test_split_no_proxy_accepts_list_or_string():
    assert netproxy.split_no_proxy(["a", " ", "b"]) == ["a", "b"]
    assert netproxy.split_no_proxy("a, b c") == ["a", "b", "c"]
    assert netproxy.split_no_proxy(None) == []


def test_with_auth_quotes_and_redact_hides_password():
    url = netproxy.with_auth("http://old:x@proxy:8080", "DOM\\me", "p@ss:w")
    assert url == "http://DOM%5Cme:p%40ss%3Aw@proxy:8080"
    assert netproxy.redact(url) == "http://DOM%5Cme:***@proxy:8080"
    assert netproxy.with_auth("http://proxy:8080", "", "x") == "http://proxy:8080"


def test_from_config_modes_and_bad_value_falls_back_to_system():
    assert netproxy.from_config({}).mode == "system"
    assert netproxy.from_config({"proxy": "direct"}).mode == "direct"
    s = netproxy.from_config({"proxy": "p:1", "no_proxy": "a,b"}, ("u", "pw"))
    assert s.mode == "manual"
    assert s.proxy == "http://u:pw@p:1"
    assert s.no_proxy == ("a", "b")
    assert netproxy.from_config({"proxy": "ftp://p:1"}).mode == "system"


def test_bypass_rules():
    assert netproxy.bypassed("http://localhost:6006/v1/traces", MANUAL)
    assert netproxy.bypassed("http://127.0.0.1:9000", MANUAL)
    assert netproxy.bypassed("https://wiki.corp.local/x", MANUAL)
    assert netproxy.bypassed("https://corp.local/x", MANUAL)
    assert not netproxy.bypassed("https://example.com/", MANUAL)
    assert not netproxy.bypassed("https://notcorp.local/", MANUAL)


def test_routes_per_mode():
    assert netproxy.proxy_for("https://x", netproxy.ProxySettings()) is None
    assert netproxy.proxy_for("https://x", netproxy.ProxySettings(proxy="direct")) == ""
    assert netproxy.proxy_for("https://x", MANUAL) == "http://proxy.corp:8080"
    assert netproxy.proxy_for("https://a.corp.local", MANUAL) == ""

    assert netproxy.httpx_kwargs("https://x", netproxy.ProxySettings()) == {}
    assert netproxy.httpx_kwargs("https://x", MANUAL) == {
        "proxy": "http://proxy.corp:8080"
    }
    assert netproxy.httpx_kwargs("http://127.0.0.1:1", MANUAL) == {"trust_env": False}

    assert netproxy.env_vars(netproxy.ProxySettings()) == {}
    env = netproxy.env_vars(MANUAL)
    assert env["HTTPS_PROXY"] == "http://proxy.corp:8080"
    assert "corp.local" in env["NO_PROXY"] and "localhost" in env["NO_PROXY"]


def test_urllib_handler_per_mode():
    assert netproxy.urllib_handler(netproxy.ProxySettings(proxy="direct")).proxies == {}
    manual = netproxy.urllib_handler(MANUAL)
    assert manual.proxies == {"http": MANUAL.proxy, "https": MANUAL.proxy}
    system = netproxy.urllib_handler(netproxy.ProxySettings())
    assert type(system) is urllib.request.ProxyHandler


def test_current_falls_back_to_saved_config(app):
    config_mod.write_config({"http": {"proxy": "p:3128"}})
    assert netproxy.current().proxy == "http://p:3128"
    netproxy.configure(netproxy.ProxySettings(proxy="direct"))
    assert netproxy.current().mode == "direct"


def test_describe_never_shows_password():
    s = netproxy.ProxySettings(proxy="http://u:secret@p:1", no_proxy=("a",))
    assert "secret" not in netproxy.describe(s)
    assert "bypass a" in netproxy.describe(s)


# ── config reader ────────────────────────────────────────────────────────────


def test_read_http_defaults_and_values(app):
    assert config_mod.read_http() == {"proxy": "", "no_proxy": [], "credential": ""}
    config_mod.write_config(
        {"http": {"proxy": "proxy:8080", "no_proxy": ["a"], "credential": " c1 "}}
    )
    assert config_mod.read_http() == {
        "proxy": "http://proxy:8080",
        "no_proxy": ["a"],
        "credential": "c1",
    }


# ── bridge (Network tab) ─────────────────────────────────────────────────────


class _Bridge(NetworkMixin):
    def __init__(self, creds=None):
        self.creds = creds or {}

    def _mcp_credential(self, cid):
        return self.creds.get(cid)


def test_save_get_roundtrip_and_apply(app):
    b = _Bridge({"px": {"username": "me", "secret": "pw"}})
    out = b.save_http_proxy_config(
        {
            "mode": "manual",
            "proxy": "proxy:8080",
            "no_proxy": "a, b",
            "credential": "px",
        }
    )
    assert out["mode"] == "manual"
    assert out["proxy"] == "http://proxy:8080"
    assert out["no_proxy"] == ["a", "b"]
    assert out["credential"] == "px"
    assert "pw" not in out["effective"]
    # Applied immediately, with the credential resolved from the keychain.
    assert netproxy.current().proxy == "http://me:pw@proxy:8080"
    # The secret never lands in config.toml.
    assert "pw" not in (app / "config.toml").read_text(encoding="utf-8")


def test_save_system_mode_removes_section(app):
    b = _Bridge()
    b.save_http_proxy_config({"mode": "direct"})
    assert config_mod.read_config()["http"] == {"proxy": "direct"}
    b.save_http_proxy_config({"mode": "system"})
    assert "http" not in config_mod.read_config()
    assert b.get_http_proxy_config()["mode"] == "system"


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "bogus"},
        {"mode": "manual", "proxy": ""},
        {"mode": "manual", "proxy": "socks5://p:1"},
    ],
)
def test_save_rejects_bad_input(app, payload):
    with pytest.raises(ValueError):
        _Bridge().save_http_proxy_config(payload)


def test_test_http_proxy_reports_validation_error():
    res = _Bridge().test_http_proxy({"mode": "manual", "proxy": ""}, "https://x")
    assert res["ok"] is False and "proxy URL" in res["error"]


# ── end to end: fetch_url through a real (local) proxy ───────────────────────


class _ProxyHandler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str]] = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("Proxy-Authorization", "")))
        body = b"via proxy"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def proxy_server():
    _ProxyHandler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_fetch_url_goes_through_manual_proxy_with_auth(proxy_server):
    port = proxy_server.server_address[1]
    settings = netproxy.from_config({"proxy": f"127.0.0.1:{port}"}, ("me", "pw"))
    res = webfetch.fetch_url("http://example.invalid/page", proxy_settings=settings)
    assert res["text"] == "via proxy"
    path, auth = _ProxyHandler.seen[-1]
    assert path == "http://example.invalid/page"  # absolute URI ⇒ proxied
    assert auth == "Basic " + base64.b64encode(b"me:pw").decode()


def test_fetch_url_uses_configured_current_settings(proxy_server):
    port = proxy_server.server_address[1]
    netproxy.configure(netproxy.from_config({"proxy": f"127.0.0.1:{port}"}))
    webfetch.fetch_url("http://example.invalid/other")
    assert _ProxyHandler.seen[-1][0] == "http://example.invalid/other"


def test_fetch_url_bypasses_proxy_for_no_proxy_host(proxy_server):
    port = proxy_server.server_address[1]
    # The proxy URL points nowhere; the target is loopback, so it's fetched
    # directly from the test server (relative path ⇒ not proxied).
    settings = netproxy.from_config({"proxy": "127.0.0.1:9"})
    res = webfetch.fetch_url(f"http://127.0.0.1:{port}/direct", proxy_settings=settings)
    assert res["text"] == "via proxy"
    assert _ProxyHandler.seen[-1][0] == "/direct"


# ── httpx clients ────────────────────────────────────────────────────────────


def test_anthropic_http_client_follows_mode():
    anthropic = pytest.importorskip("anthropic")
    from runspec_console.adapters.base import anthropic_http_client

    assert anthropic_http_client(anthropic, None) is None  # system: SDK default
    netproxy.configure(MANUAL)
    client = anthropic_http_client(anthropic, None)
    assert client is not None
    sync = anthropic_http_client(anthropic, "https://gw.corp.local", sync=True)
    assert sync is not None and sync._trust_env is False  # bypassed host ⇒ direct


def test_mcp_factory_accepts_route_kwargs():
    try:
        import httpx2  # noqa: F401 — mcp 2.x's client library
    except ImportError:
        pytest.importorskip("httpx")
    from runspec_console.mcp_client import _proxy_http_factory

    client = _proxy_http_factory({"trust_env": False})()
    assert client._trust_env is False

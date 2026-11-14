"""The webhook listener, in process: routes, auth, limits, TLS."""

from __future__ import annotations

import base64
import http.client
import json
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from runspec_scheduler import hookconfig as hc
from runspec_scheduler import listener
from runspec_scheduler import spool as sp

TOKEN = "tok-123"


class FakeWorker:
    def __init__(self) -> None:
        self.notified = 0
        self.cfg: Any = None

    def notify(self) -> None:
        self.notified += 1

    def configure(self, cfg: Any) -> None:
        self.cfg = cfg

    def running(self) -> list[Any]:
        return []


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_cfg(settings: dict[str, Any] | None = None, **hooks: dict[str, Any]) -> hc.HookConfig:
    table = {"enabled": True, "port": free_port(), **(settings or {})}
    parsed = {name: hc.parse_hook({"name": name, **raw}) for name, raw in hooks.items()}
    return hc.HookConfig(settings=hc.parse_hooks_settings(table), smtp=hc.parse_smtp(None), hooks=parsed, listed=list(parsed.values()))


DEFAULT_HOOKS = {
    "alerts": {"token_sha256": hc.sha256(TOKEN)},
    "basic": {"username": "svc", "password_hash": hc.hash_password("pw", iterations=1000)},
    "lan": {"token_sha256": hc.sha256(TOKEN), "allow": ["10.0.0.0/8"]},
    "off": {"token_sha256": hc.sha256(TOKEN), "enabled": False},
    "slow": {"token_sha256": hc.sha256(TOKEN), "rate_per_minute": 2},
}


@pytest.fixture
def service(tmp_path: Path) -> Any:
    services: list[listener.HookService] = []
    spools: list[sp.Spool] = []

    def start(settings: dict[str, Any] | None = None, hooks: dict[str, Any] | None = None) -> listener.HookService:
        spool = sp.Spool(tmp_path / f"s{len(spools)}" / "spool.sqlite")
        spools.append(spool)
        svc = listener.HookService(make_cfg(settings, **(hooks or DEFAULT_HOOKS)), spool, FakeWorker())
        svc.start()
        services.append(svc)
        return svc

    yield start
    for svc in services:
        svc.stop()
    for spool in spools:
        spool.close()


def call(svc: listener.HookService, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None, tls: bool = False) -> tuple[int, dict[str, str], str]:
    if tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn: http.client.HTTPConnection = http.client.HTTPSConnection("127.0.0.1", svc.port, timeout=5, context=ctx)
    else:
        conn = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    text = resp.read().decode()
    conn.close()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, text


def post(svc: listener.HookService, name: str, body: bytes = b'{"a":1}', **headers: str) -> tuple[int, dict[str, str], str]:
    hdrs = {"Content-Type": "application/json", **{k.replace("_", "-"): v for k, v in headers.items()}}
    return call(svc, "POST", f"/hook/{name}", body, hdrs)


def test_health_and_routes(service: Any) -> None:
    svc = service()
    assert call(svc, "GET", "/healthz")[::2] == (200, "ok")
    status, headers, _ = call(svc, "GET", "/hook/alerts")
    assert status == 405 and headers["allow"] == "POST"
    assert call(svc, "PUT", "/hook/alerts")[0] == 405
    assert call(svc, "GET", "/nope")[0] == 404
    assert call(svc, "POST", "/healthz", b"")[0] == 405
    assert call(svc, "POST", "/hook/a/b", b"")[0] == 404


def test_base_path(service: Any) -> None:
    svc = service({"base_path": "/runspec/ops"})
    assert call(svc, "GET", "/runspec/ops/healthz")[0] == 200
    assert call(svc, "GET", "/healthz")[0] == 404
    status, _, text = call(svc, "POST", "/runspec/ops/hook/alerts", b"{}", {"Authorization": f"Bearer {TOKEN}"})
    assert status == 202 and json.loads(text)["status"] == "queued"


def test_token_auth(service: Any) -> None:
    svc = service()
    assert post(svc, "alerts")[0] == 401
    assert post(svc, "alerts", Authorization="Bearer wrong")[0] == 401
    status, _, text = post(svc, "alerts", Authorization=f"Bearer {TOKEN}")
    assert status == 202
    assert post(svc, "alerts", X_Runspec_Token=TOKEN)[0] == 202
    row = svc.spool.get(json.loads(text)["id"])
    assert row and row["body"] == b'{"a":1}' and row["source"] == "127.0.0.1"
    assert "authorization" not in row["headers"] and "x-runspec-token" not in row["headers"]
    assert svc.worker.notified >= 1
    assert svc.state()["accepted"] == 2
    assert svc.state()["rejected"]


def test_basic_auth(service: Any) -> None:
    svc = service()
    status, headers, _ = post(svc, "basic")
    assert status == 401 and "basic" in headers["www-authenticate"].lower()
    good = base64.b64encode(b"svc:pw").decode()
    assert post(svc, "basic", Authorization=f"Basic {good}")[0] == 202
    assert post(svc, "basic", Authorization="Basic !!!")[0] == 401
    assert post(svc, "basic", Authorization=f"Bearer {TOKEN}")[0] == 401


def test_unknown_disabled_and_allow_list(service: Any) -> None:
    svc = service()
    assert post(svc, "ghost", Authorization=f"Bearer {TOKEN}")[0] == 404
    assert post(svc, "off", Authorization=f"Bearer {TOKEN}")[0] == 404
    assert post(svc, "lan", Authorization=f"Bearer {TOKEN}")[0] == 403


def test_trusted_proxy_forwarded_for(service: Any) -> None:
    svc = service({"trusted_proxies": ["127.0.0.0/8"]})
    status, _, _ = post(svc, "lan", Authorization=f"Bearer {TOKEN}", X_Forwarded_For="10.1.2.3, 127.0.0.1")
    assert status == 202
    # without the trust, the header is ignored
    plain = service()
    assert post(plain, "lan", Authorization=f"Bearer {TOKEN}", X_Forwarded_For="10.1.2.3")[0] == 403


def test_body_limits(service: Any) -> None:
    svc = service({"max_body": "1k"})
    status, _, text = post(svc, "alerts", b"x" * 2000, Authorization=f"Bearer {TOKEN}")
    assert status == 413 and "1024" in text
    # chunked: no Content-Length up front
    conn = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=5)
    conn.putrequest("POST", "/hook/alerts")
    conn.putheader("Authorization", f"Bearer {TOKEN}")
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    conn.send(b"2\r\n{}\r\n0\r\n\r\n")
    assert conn.getresponse().status == 411
    conn.close()


def test_rate_limit(service: Any) -> None:
    svc = service()
    codes = [post(svc, "slow", Authorization=f"Bearer {TOKEN}")[0] for _ in range(3)]
    assert codes[:2] == [202, 202]
    status, headers, _ = post(svc, "slow", Authorization=f"Bearer {TOKEN}")
    assert status == 429 and int(headers["retry-after"]) >= 1


def test_busy_spool_is_503(service: Any) -> None:
    svc = service({"max_queue": 10})
    for _ in range(10):
        svc.spool.add_delivery("alerts", source="x", content_type="", headers={}, body=b"")
    svc._busy_at = 0.0
    status, headers, text = post(svc, "alerts", Authorization=f"Bearer {TOKEN}")
    assert status == 503 and headers["retry-after"] == "30" and "waiting" in text


def test_too_many_connections(service: Any) -> None:
    svc = service({"max_connections": 1})
    hog = socket.create_connection(("127.0.0.1", svc.port))  # holds the only slot
    try:
        time.sleep(0.2)
        assert call(svc, "GET", "/healthz")[0] == 503
    finally:
        hog.close()
    deadline = time.time() + 15
    while time.time() < deadline:
        if call(svc, "GET", "/healthz")[0] == 200:
            break
        time.sleep(0.3)
    else:
        pytest.fail("the slot never came back")


def test_listen_problems_and_reconfigure(service: Any, tmp_path: Path) -> None:
    spool = sp.Spool(tmp_path / "x" / "spool.sqlite")
    svc = listener.HookService(make_cfg({"bind": "0.0.0.0"}), spool, FakeWorker())
    svc.start()
    assert svc.server is None and "needs a certificate" in svc.error
    svc.stop()
    spool.close()
    live = service()
    new = make_cfg(None, other={"token_sha256": hc.sha256(TOKEN)})
    new = hc.HookConfig(settings=live.cfg.settings, smtp=new.smtp, hooks=new.hooks, listed=new.listed)
    port = live.port
    live.reconfigure(new)
    assert live.port == port  # same listener settings: no restart
    assert post(live, "other", Authorization=f"Bearer {TOKEN}")[0] == 202
    assert post(live, "alerts", Authorization=f"Bearer {TOKEN}")[0] == 404


def test_rate_limiter() -> None:
    lim = listener.RateLimiter()
    now = 1000.0
    assert [lim.allow("k", 2, now), lim.allow("k", 2, now + 1)] == [0, 0]
    assert lim.allow("k", 2, now + 2) > 0
    assert lim.allow("k", 2, now + 61) == 0


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl to make a certificate")
def test_tls(service: Any, tmp_path: Path) -> None:
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost"],
        check=True,
        capture_output=True,
    )
    svc = service({"cert": str(cert), "key": str(key), "bind": "127.0.0.1"})
    assert svc.listening.startswith("https://")
    assert call(svc, "GET", "/healthz", tls=True)[0] == 200
    assert call(svc, "POST", "/hook/alerts", b"{}", {"Authorization": f"Bearer {TOKEN}"}, tls=True)[0] == 202
    # a plain-HTTP client and a stalled handshake don't take the listener down
    raw = socket.create_connection(("127.0.0.1", svc.port))
    raw.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
    raw.close()
    assert call(svc, "GET", "/healthz", tls=True)[0] == 200

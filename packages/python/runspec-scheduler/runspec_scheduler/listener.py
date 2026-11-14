"""listener.py — the webhook listener inside the scheduler daemon.

One HTTP(S) port per venv (``[hooks] port``). Two addresses:

* ``POST {base_path}/hook/<name>`` — a webhook. The request thread only
  checks it (address allow-list, token or basic auth, rate, size), writes one
  spool row and answers ``202 {"id": …, "status_url": …}``; everything else
  happens on the worker thread, so a flood of requests never delays a
  schedule or a run. A hook with ``respond = "output"`` holds the request
  (up to its ``respond_within``) until the run has finished, then answers
  ``200`` with the exit code and the run's standard output.
* ``GET {base_path}/hook/<name>/delivery/<id>`` — how one request went
  (same credentials as the hook).
* ``GET {base_path}/healthz`` — ``ok``, no auth (for a load balancer).

Anything else is ``404``; another method on a hook address is ``405``.

Kept robust on purpose: a cap on open connections (more are told ``503`` and
closed), a timeout on every read (a slow client can't hold a thread), the TLS
handshake done on the connection's own thread (never in the accept loop),
``Content-Length`` required and capped before anything is read, per-hook and
per-sender rate limits (``429`` + ``Retry-After``), and ``503`` while the
spool has too much waiting or the disk is nearly full. Rejections are counted
and audited at most once a minute per reason, not per request.

Stdlib only (``http.server``, ``ssl``).
"""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import json
import logging
import shutil
import socket
import ssl
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import spool as sp
from .hookconfig import HookConfig
from .hookrun import keep_headers, request_result
from .records import audit

logger = logging.getLogger("runspec_scheduler.hooks.listener")

READ_TIMEOUT = 10.0
SPOOL_CHECK_SECONDS = 2.0
#: How often a respond = "output" request looks for its run's result.
RESPOND_POLL_SECONDS = 0.2


class RateLimiter:
    """A sliding one-minute window per key."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, per_minute: int, now: float | None = None) -> float:
        """0 when allowed (and counted), else seconds until it would be."""
        now = now or time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if len(hits) >= per_minute:
                return max(1.0, 60 - (now - hits[0]))
            hits.append(now)
            return 0.0


class Rejections:
    """Counts rejected requests; audits each reason at most once a minute."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)
        self._pending: dict[tuple[str, str], int] = defaultdict(int)
        self._last: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def add(self, hook: str, reason: str, source: str) -> None:
        key = (hook, reason)
        now = time.monotonic()
        with self._lock:
            self.counts[reason] += 1
            self._pending[key] += 1
            if now - self._last.get(key, 0.0) < 60:
                return
            count, self._pending[key] = self._pending[key], 0
            self._last[key] = now
        logger.warning("webhook %s: rejected %d request(s): %s (last from %s)", hook or "-", count, reason, source)
        audit("hook.rejected", "denied", target={"hook": hook or None}, detail={"reason": reason, "count": count, "source": source})


class HookServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], service: HookService) -> None:
        self.service = service
        self._slots = threading.BoundedSemaphore(service.cfg.settings.max_connections)
        family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.address_family = family
        super().__init__(address, HookHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            # Too many open connections: say so and close, without a thread.
            with contextlib.suppress(OSError):
                if not isinstance(request, ssl.SSLSocket):
                    request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nRetry-After: 5\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            self.shutdown_request(request)
            self.service.rejections.add("", "too many connections", str(client_address[0]))
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        logger.debug("webhook connection from %s failed", client_address, exc_info=True)


class HookHandler(BaseHTTPRequestHandler):
    server: HookServer
    protocol_version = "HTTP/1.0"
    timeout = READ_TIMEOUT
    server_version = "runspec-scheduler"
    sys_version = ""

    def setup(self) -> None:
        self.request.settimeout(READ_TIMEOUT)
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()
        super().setup()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — the stdlib's name
        logger.debug("%s %s", self.client_address[0], format % args)

    # helpers

    def _send(self, code: int, payload: dict[str, Any] | str | None = None, headers: dict[str, str] | None = None) -> None:
        if isinstance(payload, dict):
            body = json.dumps(payload).encode()
            ctype = "application/json"
        else:
            body = (payload or "").encode()
            ctype = "text/plain; charset=utf-8"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self) -> tuple[str, str]:
        """(kind, hook name): kind is ``health``, ``hook``, ``delivery`` (the
        name is then ``<hook>/<id>``) or ``""``."""
        path = self.path.split("?", 1)[0]
        base = self.server.service.cfg.settings.base_path
        if base:
            if not path.startswith(base + "/"):
                return "", ""
            path = path[len(base) :]
        if path == "/healthz":
            return "health", ""
        if path.startswith("/hook/") and path.count("/") == 2 and len(path) > 6:
            return "hook", path[6:]
        parts = path.split("/")
        if len(parts) == 5 and parts[1] == "hook" and parts[2] and parts[3] == "delivery" and parts[4].isdigit():
            return "delivery", f"{parts[2]}/{parts[4]}"
        return "", ""

    def _status_url(self, name: str, delivery: int) -> str:
        """Where a sender asks how a request went (relative to this listener)."""
        return f"{self.server.service.cfg.settings.base_path}/hook/{name}/delivery/{delivery}"

    def _checked_hook(self, name: str, source: str) -> Any:
        """The hook when this sender may use it, else None (the answer is sent)."""
        service = self.server.service
        hook = service.cfg.hooks.get(name)
        if hook is None or not hook.enabled:
            service.rejections.add(name, "unknown or disabled hook", source)
            self._send(404, "not found")
            return None
        if not hook.allows(source):
            service.rejections.add(name, "address not allowed", source)
            self._send(403, "forbidden")
            return None
        if not _authorised(hook, self.headers):
            service.rejections.add(name, "bad or missing credentials", source)
            extra = {"WWW-Authenticate": 'Basic realm="runspec"'} if hook.username else None
            self._send(401, "unauthorised", extra)
            return None
        return hook

    def _status(self, key: str) -> None:
        """``GET …/hook/<name>/delivery/<id>``: how one request went (same
        credentials as posting it)."""
        service = self.server.service
        name, _, raw_id = key.partition("/")
        source = self._source()
        hook = self._checked_hook(name, source)
        if hook is None:
            return
        wait = service.limiter.allow(f"status:{name}", max(60, 4 * (hook.rate_per_minute or service.cfg.settings.rate_per_minute)))
        if wait:
            self._send(429, "too many requests", {"Retry-After": str(int(wait))})
            return
        result = request_result(service.spool, int(raw_id), hook=hook, status_url=self._status_url(name, int(raw_id)))
        if result is None or result.get("hook") != name:
            self._send(404, "not found")
            return
        self._send(200, result)

    def _source(self) -> str:
        """The sender's address: the peer, or (when the peer is a trusted
        proxy) the last X-Forwarded-For address that isn't one."""
        peer = self.client_address[0]
        trusted = self.server.service.cfg.settings.trusted_proxies
        if not trusted or not _in(peer, trusted):
            return peer
        chain = [p.strip() for p in (self.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
        for addr in reversed(chain):
            if not _in(addr, trusted):
                return addr
        return peer

    # methods

    def do_GET(self) -> None:  # noqa: N802 — the stdlib's name
        kind, name = self._route()
        if kind == "health":
            self._send(200, "ok")
        elif kind == "delivery":
            self._status(name)
        elif kind == "hook":
            self._send(405, "POST only", {"Allow": "POST"})
        else:
            self._send(404, "not found")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _not_post(self) -> None:
        kind, _ = self._route()
        if kind == "delivery":
            self._send(405, "GET only", {"Allow": "GET"})
            return
        self._send(405 if kind else 404, "POST only" if kind else "not found", {"Allow": "POST" if kind == "hook" else "GET"} if kind else None)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_post  # noqa: N815

    def do_POST(self) -> None:  # noqa: N802
        service = self.server.service
        cfg = service.cfg
        kind, name = self._route()
        if kind != "hook":
            self._send(404 if kind == "" else 405, "not found" if kind == "" else "GET only")
            return
        source = self._source()
        hook = self._checked_hook(name, source)
        if hook is None:
            return
        wait = service.limiter.allow(f"hook:{name}", hook.rate_per_minute or cfg.settings.rate_per_minute) or service.limiter.allow(f"src:{source}", cfg.settings.source_rate_per_minute)
        if wait:
            service.rejections.add(name, "rate limit", source)
            self._send(429, "too many requests", {"Retry-After": str(int(wait))})
            return
        if (self.headers.get("Transfer-Encoding") or "").lower() not in ("", "identity"):
            self._send(411, "send a Content-Length")
            return
        try:
            length = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            length = -1
        if length < 0:
            self._send(411, "send a Content-Length")
            return
        if length > cfg.settings.max_body:
            service.rejections.add(name, "body too large", source)
            self._send(413, f"the body is over {cfg.settings.max_body} bytes")
            return
        busy = service.spool_busy()
        if busy:
            service.rejections.add(name, busy, source)
            self._send(503, busy, {"Retry-After": "30"})
            return
        try:
            body = self.rfile.read(length) if length else b""
        except (TimeoutError, OSError):
            service.rejections.add(name, "the client was too slow sending the body", source)
            return
        if len(body) != length:
            self._send(400, "the body was shorter than its Content-Length")
            return
        headers = keep_headers({k: v for k, v in self.headers.items()})
        try:
            delivery = service.spool.add_delivery(name, source=source, content_type=self.headers.get("Content-Type") or "", headers=headers, body=body)
        except Exception as exc:  # noqa: BLE001 — a spool failure is a 503, never a crash
            logger.error("webhook %s: couldn't spool a delivery: %s", name, exc)
            self._send(503, "can't store it right now", {"Retry-After": "30"})
            return
        service.accepted += 1
        service.notify()
        status_url = self._status_url(name, delivery)
        if hook.respond != "output":
            self._send(202, {"id": delivery, "status": "queued", "status_url": status_url})
            return
        # respond = "output": hold the request until every item has settled
        # (or waits for an approval), up to respond_within; then answer with
        # what's known — 200 when done, else 202 and the status URL.
        deadline = time.monotonic() + hook.respond_within
        result = None
        while True:
            try:
                result = request_result(service.spool, delivery, hook=hook, status_url=status_url)
            except Exception as exc:  # noqa: BLE001 — a read hiccup: keep the 202 promise
                logger.debug("webhook %s: reading delivery %d failed: %s", name, delivery, exc)
            if (result and result["status"] != "running") or time.monotonic() >= deadline:
                break
            time.sleep(RESPOND_POLL_SECONDS)
        if result is None:
            self._send(202, {"id": delivery, "status": "queued", "status_url": status_url})
        else:
            self._send(200 if result["status"] == "done" else 202, result)


def _in(address: str, networks: tuple[Any, ...]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in networks)


def _authorised(hook: Any, headers: Any) -> bool:
    auth = headers.get("Authorization") or ""
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and hook.check_token(value.strip()):
        return True
    if hook.token_sha256 and hook.check_token((headers.get(hook.token_header) or "").strip()):
        return True
    if scheme.lower() == "basic" and hook.username:
        try:
            user, _, password = base64.b64decode(value.strip(), validate=True).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return bool(hook.check_basic(user, password))
    return False


class HookService:
    """The listener + the spool + the worker, started and reloaded by the
    daemon. ``state()`` goes in the daemon's heartbeat."""

    def __init__(self, cfg: HookConfig, spool: sp.Spool, worker: Any) -> None:
        self.cfg = cfg
        self.spool = spool
        self.worker = worker
        self.limiter = RateLimiter()
        self.rejections = Rejections()
        self.accepted = 0
        self.server: HookServer | None = None
        self.thread: threading.Thread | None = None
        self.error: str = ""
        self.listening: str = ""
        self._busy_at = 0.0
        self._busy = ""
        self._busy_lock = threading.Lock()

    def notify(self) -> None:
        self.worker.notify()

    def spool_busy(self) -> str:
        """Why the spool shouldn't take more right now, or "" (checked at most
        every couple of seconds)."""
        now = time.monotonic()
        with self._busy_lock:
            if now - self._busy_at < SPOOL_CHECK_SECONDS:
                return self._busy
            self._busy_at = now
            reason = ""
            try:
                waiting = self.spool.count(sp.QUEUED)
                if waiting >= self.cfg.settings.max_queue:
                    reason = f"{waiting} deliveries are waiting to be worked through"
                elif self.cfg.settings.min_free_mb:
                    free = shutil.disk_usage(self.spool.path.parent).free // (1024 * 1024)
                    if free < self.cfg.settings.min_free_mb:
                        reason = f"only {free} MB free where the spool is"
            except Exception as exc:  # noqa: BLE001 — can't tell = refuse, safely
                reason = f"the spool can't be read: {exc}"
            self._busy = reason
            return reason

    def start(self) -> None:
        settings = self.cfg.settings
        problem = settings.listen_problem()
        if problem:
            self.error = problem if settings.enabled else ""
            return
        try:
            server = HookServer((settings.bind, settings.port), self)
            if settings.tls:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                ctx.load_cert_chain(settings.cert, settings.key)
                server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        except (OSError, ssl.SSLError) as exc:
            self.error = f"can't listen on {settings.bind}:{settings.port}: {exc}"
            logger.error("webhooks: %s", self.error)
            audit("hook.listener", "failed", detail={"error": self.error})
            return
        self.server = server
        self.error = ""
        self.listening = f"{'https' if settings.tls else 'http'}://{settings.bind}:{server.server_address[1]}{settings.base_path}"
        self.thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="runspec-hooks-listener", daemon=True)
        self.thread.start()
        logger.info("webhooks: listening on %s (%d hooks)", self.listening, len(self.cfg.hooks))
        audit("hook.listener", "started", detail={"address": self.listening, "hooks": sorted(self.cfg.hooks)})

    @property
    def port(self) -> int:
        return int(self.server.server_address[1]) if self.server else 0

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            logger.info("webhooks: stopped listening")
        self.listening = ""

    def reconfigure(self, cfg: HookConfig) -> None:
        """New hooks take effect at once; new listener settings restart it."""
        restart = cfg.settings != self.cfg.settings or not self.server
        self.cfg = cfg
        self.worker.configure(cfg)
        if restart:
            self.stop()
            self.start()

    def state(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.settings.enabled,
            "listening": self.listening or None,
            "error": self.error or None,
            "hooks": sorted(self.cfg.hooks),
            "accepted": self.accepted,
            "rejected": dict(self.rejections.counts),
            "running": self.worker.running(),
        }

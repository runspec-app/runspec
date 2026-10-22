"""Re-make the sample capture the tests and shipped mocks are built from.

Captures made-up traffic on the loopback interface, then rewrites the addresses
so the capture reads like a small office network:

    10.20.0.15   client (workstation)
    10.20.0.53   DNS server
    10.20.1.80   web server (plain HTTP on 8080; nothing listens on 9999)
    10.20.1.43   API server (TLS on 8443, self-signed cert for api.example.com)

Traffic: DNS lookups (one answered, one NXDOMAIN), HTTP requests (200, 404,
500, a POST), a refused connection (RST), a TLS 1.3 session, a TLS 1.2 session,
and a TLS handshake the client aborts with an ``unknown_ca`` alert. The TLS 1.3
session's keys go to ``sample.keys`` so ``--keylog-file`` can be tested.

Needs root (for loopback capture and port 53), tshark, and scapy:

    sudo python scripts/make_sample_capture.py

Writes tests/data/sample.pcapng and tests/data/sample.keys.
"""

from __future__ import annotations

import contextlib
import http.server
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "tests" / "data"
CLIENT, DNS, WEB, API = "127.0.0.10", "127.0.0.53", "127.0.0.20", "127.0.0.30"
REWRITE = {CLIENT: "10.20.0.15", DNS: "10.20.0.53", WEB: "10.20.1.80", API: "10.20.1.43"}
MACS = {CLIENT: "02:00:0a:14:00:0f", DNS: "02:00:0a:14:00:35", WEB: "02:00:0a:14:01:50", API: "02:00:0a:14:01:2b"}


# ── DNS: a tiny responder that knows one name ───────────────────────────────


def _qname(packet: bytes) -> tuple[str, int]:
    labels, i = [], 12
    while packet[i]:
        n = packet[i]
        labels.append(packet[i + 1 : i + 1 + n].decode())
        i += n + 1
    return ".".join(labels), i + 5


def _dns_server(sock: socket.socket) -> None:
    while True:
        try:
            packet, peer = sock.recvfrom(512)
        except OSError:
            return
        name, end = _qname(packet)
        question = packet[12:end]
        if name == "api.example.com":
            answer = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 300, 4) + socket.inet_aton(REWRITE[API])
            header = packet[:2] + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0)
            sock.sendto(header + question + answer, peer)
        else:
            sock.sendto(packet[:2] + struct.pack(">HHHHH", 0x8183, 1, 0, 0, 0) + question, peer)


def _dns_query(name: str, qid: int) -> None:
    query = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    query += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00" + struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((CLIENT, 0))
        s.settimeout(2)
        s.sendto(query, (DNS, 53))
        s.recvfrom(512)


# ── HTTP ────────────────────────────────────────────────────────────────────


class _Web(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "nginx"
    sys_version = ""

    def _reply(self, status: int, body: bytes, ctype: str = "text/html") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._reply(200, b"<html><body><h1>Orders portal</h1></body></html>")
        elif self.path.startswith("/api/orders"):
            time.sleep(0.05)
            self._reply(500, b'{"error":"database timeout"}', "application/json")
        else:
            self._reply(404, b"<html><body>Not found</body></html>")

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._reply(201, b'{"id":4711,"status":"created"}', "application/json")

    def log_message(self, *args: object) -> None:
        pass


def _http(method: str, path: str, body: bytes | None = None) -> None:
    handler = urllib.request.HTTPHandler()
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(f"http://{WEB}:8080{path}", data=body, method=method, headers={"Host": "portal.example.com", "User-Agent": "curl/8.5.0"})
    if body:
        req.add_header("Content-Type", "application/json")
    # Bind the client side to CLIENT.
    orig = socket.create_connection

    def bound(address, timeout=None, source_address=None, **kw):  # type: ignore[no-untyped-def]
        return orig(address, timeout=timeout, source_address=(CLIENT, 0))

    socket.create_connection = bound  # type: ignore[assignment]
    try:
        try:
            opener.open(req, timeout=3).read()
        except urllib.error.HTTPError as exc:
            exc.read()
    finally:
        socket.create_connection = orig  # type: ignore[assignment]


def _refused() -> None:
    with socket.socket() as s:
        s.bind((CLIENT, 0))
        with contextlib.suppress(ConnectionRefusedError):
            s.connect((WEB, 9999))


# ── TLS ─────────────────────────────────────────────────────────────────────


def _make_cert(tmp: str) -> tuple[str, str]:
    key, cert = os.path.join(tmp, "key.pem"), os.path.join(tmp, "cert.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "30", "-subj", "/CN=api.example.com/O=Example Corp"],
        check=True,
        capture_output=True,
    )
    return cert, key


def _tls_server(sock: socket.socket, ctx: ssl.SSLContext) -> None:
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        try:
            with ctx.wrap_socket(conn, server_side=True) as tls:
                data = tls.recv(4096)
                if data:
                    tls.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 16\r\nConnection: close\r\n\r\n{"healthy":true}')
        except (ssl.SSLError, OSError):
            pass


def _tls_client(*, verify: bool, version: ssl.TLSVersion | None = None, keylog: str | None = None) -> None:
    ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()  # noqa: S323
    if version:
        ctx.maximum_version = version
    if keylog:
        ctx.keylog_filename = keylog
    with socket.socket() as raw:
        raw.bind((CLIENT, 0))
        raw.connect((API, 8443))
        try:
            with ctx.wrap_socket(raw, server_hostname="api.example.com") as tls:
                tls.sendall(b"GET /health HTTP/1.1\r\nHost: api.example.com\r\nConnection: close\r\n\r\n")
                while tls.recv(4096):
                    pass
        except ssl.SSLError:
            pass


# ── rewrite ─────────────────────────────────────────────────────────────────


def _rewrite(src: str, dst: str) -> None:
    from scapy.all import IP, TCP, UDP, Ether, rdpcap, wrpcapng  # type: ignore[import-not-found]

    out = []
    for pkt in rdpcap(src):
        if IP in pkt:
            ip = pkt[IP]
            if Ether in pkt:
                pkt[Ether].src, pkt[Ether].dst = MACS.get(ip.src, pkt[Ether].src), MACS.get(ip.dst, pkt[Ether].dst)
            ip.src, ip.dst = REWRITE.get(ip.src, ip.src), REWRITE.get(ip.dst, ip.dst)
            del ip.chksum
            for layer in (TCP, UDP):
                if layer in pkt:
                    del pkt[layer].chksum
        out.append(pkt)
    wrpcapng(dst, out)


def main() -> None:
    if os.geteuid() != 0:
        sys.exit("run as root (loopback capture + port 53)")
    if not shutil.which("tshark"):
        sys.exit("tshark not on PATH")
    DATA.mkdir(parents=True, exist_ok=True)
    keys = DATA / "sample.keys"
    keys.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "raw.pcapng")
        hosts = " or ".join(f"host {h}" for h in REWRITE)
        cap = subprocess.Popen(["tshark", "-q", "-i", "lo", "-f", hosts, "-w", raw], stderr=subprocess.DEVNULL)
        time.sleep(2)

        dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dns.bind((DNS, 53))
        threading.Thread(target=_dns_server, args=(dns,), daemon=True).start()
        web = http.server.ThreadingHTTPServer((WEB, 8080), _Web)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        cert, key = _make_cert(tmp)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        api = socket.socket()
        api.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        api.bind((API, 8443))
        api.listen()
        threading.Thread(target=_tls_server, args=(api, ctx), daemon=True).start()

        _dns_query("api.example.com", 0x1A2B)
        _dns_query("portal.example.com", 0x1A2C)
        _dns_query("nope.example.com", 0x1A2D)
        for method, path, body in [("GET", "/", None), ("GET", "/favicon.ico", None), ("GET", "/api/orders?day=today", None), ("POST", "/api/orders", b'{"sku":"A-100","qty":2}')]:
            _http(method, path, body)
            time.sleep(0.1)
        _refused()
        _tls_client(verify=False, keylog=str(keys))
        _tls_client(verify=False, version=ssl.TLSVersion.TLSv1_2)
        _tls_client(verify=True)
        time.sleep(1.5)

        cap.terminate()
        cap.wait()
        web.shutdown()
        dns.close()
        api.close()
        _rewrite(raw, str(DATA / "sample.pcapng"))
    print(f"wrote {DATA / 'sample.pcapng'} and {keys}")


if __name__ == "__main__":
    main()

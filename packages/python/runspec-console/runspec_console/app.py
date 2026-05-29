"""
app.py — pywebview entry point for runspec-console.

Dev mode  (--dev):  points the window at the Vite dev server (http://localhost:<port>)
Prod mode (default): serves the built Vite dist folder via a local HTTP server.

Usage:
  runspec-console            # production — requires packages/console-ui/dist/
  runspec-console --dev      # development — requires `npm run dev` running
  runspec-console --dev --port 5174
"""

from __future__ import annotations

import sys
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path


def _find_dist() -> Path:
    """Locate the built console-ui dist folder relative to this package."""
    candidates = [
        # Installed package data (future: bundle dist into the wheel)
        Path(__file__).parent / "dist",
        # Development: sibling packages directory
        Path(__file__).parents[3] / "console-ui" / "dist",
    ]
    for c in candidates:
        if (c / "index.html").exists():
            return c
    raise FileNotFoundError(
        "console-ui dist not found. Run `npm run build` in packages/console-ui, "
        "or use --dev to point at the Vite dev server instead."
    )


def _start_static_server(dist: Path) -> int:
    """Serve dist/ on a random free port. Returns the port number."""
    import socket

    class _Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a: object, **kw: object) -> None:
            super().__init__(*a, directory=str(dist), **kw)

        def log_message(self, *_: object) -> None:
            pass  # silence request logs

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = HTTPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port


def _build_icon() -> Path | None:
    """Generate a simple runspec-branded .ico on first launch, cached in app-data."""
    import struct
    import zlib

    from .config import config_path

    cache = config_path().parent / "runspec_console.ico"
    if cache.exists():
        return cache
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        r, g, b, w = 9, 88, 217, 32  # runspec blue, 32×32

        def _chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        scanlines = b"".join(b"\x00" + bytes([r, g, b] * w) for _ in range(w))
        png = (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, w, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(scanlines))
            + _chunk(b"IEND", b"")
        )
        ico = (
            struct.pack("<HHH", 0, 1, 1)
            + struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(png), 22)
            + png
        )
        cache.write_bytes(ico)
        return cache
    except Exception:
        return None


def main() -> None:
    import runspec

    args = runspec.parse("runspec-console")
    dev: bool = bool(args.dev.value)
    port: int = int(args.port.value) if args.port.value is not None else 5173

    import webview

    from .bridge import Bridge

    bridge = Bridge()

    if dev:
        url = f"http://localhost:{port}"
    else:
        try:
            dist = _find_dist()
            static_port = _start_static_server(dist)
            url = f"http://127.0.0.1:{static_port}"
        except FileNotFoundError as exc:
            print(f"✗  {exc}", file=sys.stderr)
            sys.exit(1)

    window = webview.create_window(
        "runspec console",
        url,
        js_api=bridge,
        width=1440,
        height=900,
        min_size=(1024, 600),
        frameless=True,
    )
    bridge.set_window(window)

    start_kwargs: dict[str, object] = {"debug": dev}
    icon = _build_icon()
    if icon:
        start_kwargs["icon"] = str(icon)
    webview.start(**start_kwargs)

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

import os
import sys
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path


def _resolve_debug(dev: bool, devtools_arg: bool) -> bool:
    """Whether to enable the Chromium inspector.

    `--dev` implies it; `--devtools` or RUNSPEC_CONSOLE_DEVTOOLS=1 opt into it for
    production (bundled-dist) builds where the inspector is otherwise off.
    """
    return dev or devtools_arg or os.environ.get("RUNSPEC_CONSOLE_DEVTOOLS") == "1"


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


def _apply_dwm_title_bar(title: str) -> None:
    """Style the native Windows title bar: dark mode + runspec-blue caption color.

    Called from the webview `started` callback so the window HWND is available.
    Requires Windows 11 build 22000+ for DWMWA_CAPTION_COLOR (attr 35).
    """
    import ctypes
    import time

    try:
        import win32gui
    except ImportError:
        return

    # Brief wait for the window to be fully painted before we style it
    time.sleep(0.15)
    hwnd = win32gui.FindWindow(None, title)
    if not hwnd:
        return

    try:
        dwm = ctypes.windll.dwmapi

        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        dark = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))

        # DWMWA_CAPTION_COLOR = 35  (COLORREF is 0x00BBGGRR)
        # Runspec blue #0958D9 = R=0x09 G=0x58 B=0xD9 → 0x00D95809
        blue = ctypes.c_int(0x00D95809)
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(blue), ctypes.sizeof(blue))

        # DWMWA_TEXT_COLOR = 36 — white
        white = ctypes.c_int(0x00FFFFFF)
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(white), ctypes.sizeof(white))
    except Exception:
        pass


def _suppress_noisy_loggers() -> None:
    """Quiet paramiko before the Bridge attaches its Dev-tab log forwarder.

    Once ``Bridge`` is constructed it re-routes the paramiko loggers into the
    Dev tab (``runspec:ssh`` events) and turns off propagation to the terminal;
    this pre-window default keeps any stray pre-Bridge record off the console.
    """
    import logging

    for name in ("paramiko", "paramiko.transport"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    _suppress_noisy_loggers()

    import runspec

    args = runspec.parse("runspec-console")
    dev: bool = bool(args.dev.value)
    port: int = int(args.port.value) if args.port.value is not None else 5173
    debug_enabled: bool = _resolve_debug(dev, bool(args.devtools.value))

    import webview

    from .bridge import Bridge

    bridge = Bridge()
    bridge.set_debug(debug_enabled)

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

    window_title = "runspec console"
    window = webview.create_window(
        window_title,
        url,
        js_api=bridge,
        width=1440,
        height=900,
        min_size=(1024, 600),
    )
    bridge.set_window(window)

    start_kwargs: dict[str, object] = {
        "debug": debug_enabled,
        "func": lambda: _apply_dwm_title_bar(window_title),
    }
    icon = _build_icon()
    if icon:
        start_kwargs["icon"] = str(icon)
    webview.start(**start_kwargs)

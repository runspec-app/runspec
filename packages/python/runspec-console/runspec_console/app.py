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

import logging
import os
import sys
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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


def _safe_getattr(obj: Any, name: str) -> Any:
    """``getattr(obj, name, None)`` that also swallows non-``AttributeError``.

    The objects in the WebView2 graph are .NET/pythonnet proxies whose property
    getters can *throw* — e.g. ``CoreWebView2`` raises ``System.InvalidCastException``
    when probed before the control is fully initialised or off its UI thread. Plain
    ``getattr(obj, name, None)`` only suppresses ``AttributeError``, so such a throw
    would abort the whole BFS. Treat any failure as "attribute absent" so one bad
    node can't hide a good ``CoreWebView2`` elsewhere in the graph."""
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _find_core_webview2(window: Any) -> tuple[Any, str]:
    """Walk the pywebview window's object graph for the WebView2 ``CoreWebView2``.

    pywebview's EdgeChromium control sits behind version-dependent attributes
    (``native`` / ``native.web_view`` / ``native.browser.web_view`` / …), so rather
    than guess one path we breadth-first search a curated set of child attributes
    (never arbitrary ``dir()`` members — those could invoke .NET properties with
    side effects). Every probe goes through :func:`_safe_getattr` so a property
    getter that *throws* (a real WebView2 hazard) is skipped, not fatal. Returns
    ``(core, dotted_path)`` or ``(None, "")``."""
    child_attrs = (
        "native",
        "browser",
        "web_view",
        "webview",
        "control",
        "form",
        "gui",
    )
    queue: list[tuple[Any, str]] = [(window, "window")]
    seen: set[int] = set()
    while queue:
        obj, path = queue.pop(0)
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        core = _safe_getattr(obj, "CoreWebView2")
        if core is not None:
            return core, path
        for attr in child_attrs:
            child = _safe_getattr(obj, attr)
            if child is not None:
                queue.append((child, f"{path}.{attr}"))
    return None, ""


def _enable_native_context_menu(window: Any, timeout: float = 5.0) -> None:
    """Turn on the WebView2 default right-click menu (Windows / EdgeChromium).

    pywebview disables the WebView2 default context menu unless debug mode is on,
    which also removes the menu's Cut/Copy/Paste **and the spell-checker
    suggestions** for a misspelled word in an editable field (the prompt box). The
    red squiggles are WebView2's spellcheck — but the *suggestions* only surface
    through that native menu, and there is no JS API to reach them, so re-enabling
    the default menu is the only way to get right-click copy/paste + spelling fixes.

    Best-effort and fully guarded: the ``CoreWebView2`` initialises asynchronously,
    so we poll briefly for it, then flip ``AreDefaultContextMenusEnabled``. When it
    can't be found we print a one-line diagnostic to stderr (the control's type +
    attributes) so the actual object path can be pinned on a real install — this is
    Windows-GUI internals that can't be exercised off-Windows. Any failure is
    otherwise a silent no-op, like :func:`_apply_dwm_title_bar`."""
    import time

    try:
        deadline = time.monotonic() + timeout
        core, path = _find_core_webview2(window)
        while core is None and time.monotonic() < deadline:
            time.sleep(0.1)
            core, path = _find_core_webview2(window)
        if core is None:
            native = getattr(window, "native", None)
            attrs = [a for a in dir(native) if not a.startswith("_")] if native else []
            print(
                "runspec-console: WebView2 CoreWebView2 not found — right-click "
                f"menu/spellcheck stay off. native={type(native).__name__} "
                f"attrs={attrs[:60]}",
                file=sys.stderr,
            )
            return
        core.Settings.AreDefaultContextMenusEnabled = True
        logger.info("WebView2 default context menu enabled (via %s)", path)
    except Exception:
        logger.info("could not enable WebView2 context menu", exc_info=True)


def _on_started(window: Any, title: str) -> None:
    """webview ``started`` hook: native title-bar styling + context menu."""
    _apply_dwm_title_bar(title)
    _enable_native_context_menu(window)


def main() -> None:
    _suppress_noisy_loggers()

    import runspec

    args = runspec.parse("runspec-console")
    dev: bool = bool(args.dev.value)
    port: int = int(args.port.value) if args.port.value is not None else 5173
    debug_enabled: bool = _resolve_debug(dev, bool(args.devtools.value))

    import webview

    from . import notify
    from .bridge import Bridge

    # Pin the AppUserModelID before any toast so confirm notifications carry a
    # stable runspec identity in the Action Center.
    notify.set_app_id()

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

    # White-label the native window title to the active LLM plugin's package
    # (see branding.py). Resolve once and share it with the Bridge so its
    # FindWindow-based taskbar flash / focus helpers match exactly.
    from .branding import resolve_branding

    window_title = resolve_branding().title
    bridge.set_window_title(window_title)
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
        "func": lambda: _on_started(window, window_title),
    }
    icon = _build_icon()
    if icon:
        start_kwargs["icon"] = str(icon)
    webview.start(**start_kwargs)

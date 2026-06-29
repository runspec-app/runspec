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
    """Whether to auto-open the Chromium devtools window.

    `--dev` implies it; `--devtools` or RUNSPEC_CONSOLE_DEVTOOLS=1 opt into it for
    production (bundled-dist) builds where the inspector is otherwise off. Note the
    WebView2 right-click menu + spellcheck are on regardless (see
    :func:`_enable_native_context_menu`); this only controls the devtools *window*.
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


def _icon_rgb() -> tuple[int, int, int]:
    """The icon square's colour: the white-label `[console.theme] accent_color`
    when set (so the app icon matches the brand), else runspec blue."""
    from .config import theme_config, valid_accent

    accent = valid_accent(theme_config().get("accent_color"))
    if accent:
        h = accent.lstrip("#")
        if len(h) == 3:  # #rgb → #rrggbb
            h = "".join(c * 2 for c in h)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 9, 88, 217  # runspec blue


def _build_icon() -> Path | None:
    """Generate a simple branded .ico, cached in app-data.

    A white-label install can override the icon outright with `[console.theme]
    icon` (a readable `.ico`/`.png` path); otherwise a solid 32×32 square is
    painted in `accent_color` (the brand colour) or runspec blue. The cache key
    includes the colour, so changing `accent_color` regenerates it."""
    import struct
    import zlib

    from .config import config_path, theme_config

    # An explicit icon path wins — used verbatim.
    icon_override = theme_config().get("icon")
    if isinstance(icon_override, str) and icon_override.strip():
        p = Path(icon_override.strip()).expanduser()
        if p.is_file():
            return p

    r, g, b = _icon_rgb()
    w = 32
    cache = config_path().parent / f"runspec_console-{r:02x}{g:02x}{b:02x}.ico"
    if cache.exists():
        return cache
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)

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


def _enable_native_context_menu(webview_module: Any, *, open_devtools: bool) -> bool:
    """Configure pywebview so the WebView2 default right-click menu is available.

    That native menu carries Cut/Copy/Paste **and the spell-checker suggestions**
    for a misspelled word in an editable field (the prompt box). The red squiggles
    are WebView2's own spellcheck, but the *suggestions* only surface through the
    default context menu — there is no JS API to reach them.

    pywebview already does the right thing here: its EdgeChromium backend sets
    ``CoreWebView2.Settings.AreDefaultContextMenusEnabled = debug`` itself, on the
    UI thread during control init, and opens the *devtools window* only when the
    **separate** ``OPEN_DEVTOOLS_IN_DEBUG`` setting is also true. So we run
    pywebview in debug mode (which enables the menu + spellcheck) but pin
    ``OPEN_DEVTOOLS_IN_DEBUG`` to the user's actual devtools preference — the menu
    comes on without the inspector popping open.

    This replaces an earlier approach that walked the live WebView2 object graph
    from pywebview's background ``started`` thread to flip the setting after the
    fact; touching those UI-thread-affine .NET objects off-thread intermittently
    deadlocked the window at startup. Using the library's own settings removes the
    background-thread access entirely, so it cannot hang.

    Returns the ``debug`` value to pass to :func:`webview.start` (always ``True``)."""
    webview_module.settings["OPEN_DEVTOOLS_IN_DEBUG"] = open_devtools
    return True


def _on_started(title: str) -> None:
    """webview ``started`` hook: native title-bar styling."""
    _apply_dwm_title_bar(title)


def main() -> None:
    _suppress_noisy_loggers()

    import runspec

    args = runspec.parse("runspec-console")
    dev: bool = bool(args.dev.value)
    port: int = int(args.port.value) if args.port.value is not None else 5173
    debug_enabled: bool = _resolve_debug(dev, bool(args.devtools.value))

    import webview

    # Run pywebview in debug mode so the WebView2 default right-click menu +
    # spell-check suggestions are on (see _enable_native_context_menu), but keep
    # the devtools window gated on the user's --dev/--devtools choice. Pin
    # PYWEBVIEW_LOG first so debug mode doesn't crank pywebview's logger to DEBUG.
    os.environ.setdefault("PYWEBVIEW_LOG", "WARNING")
    logging.getLogger("pywebview").setLevel(logging.WARNING)
    webview_debug = _enable_native_context_menu(webview, open_devtools=debug_enabled)

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
        # Paint the window the dark-theme content colour (App.tsx contentBg) so
        # there's no white flash before the SPA mounts.
        background_color="#111111",
        # Native "are you sure?" guard so an accidental close doesn't drop an
        # in-progress agent run or chat.
        confirm_close=True,
        # Let users select & copy arbitrary page text (not just inputs/outputs).
        text_select=True,
        # Ctrl+scroll / pinch to zoom the whole UI (readability / accessibility).
        zoomable=True,
    )
    bridge.set_window(window)

    start_kwargs: dict[str, object] = {
        "debug": webview_debug,
        "func": lambda: _on_started(window_title),
    }
    icon = _build_icon()
    if icon:
        start_kwargs["icon"] = str(icon)
    webview.start(**start_kwargs)

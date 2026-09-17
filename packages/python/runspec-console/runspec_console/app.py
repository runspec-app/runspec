"""
app.py — pywebview entry point for runspec-console.

Dev mode  (--dev):  points the window at the Vite dev server (http://localhost:<port>)
Prod mode (default): serves the built Vite dist folder via a local HTTP server.

Usage:
  runspec-console-app        # windowed launcher (no console window; gui-scripts → main_gui)
  runspec-console            # console launcher — same app, logs to the terminal
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


def _resolve_icon_override(value: str) -> Path | None:
    """Resolve a `[console.theme] icon` value to a real filesystem path, or None.

    Two forms:

    * a plain filesystem path (``C:/brand/app.ico`` / ``~/app.ico``) — used as-is
      when it's a readable file;
    * a **package reference** ``pkg:<module>:<relpath>`` (e.g.
      ``pkg:mycorp_console:app.ico`` or ``pkg:mycorp_console:assets/app.ico``) —
      the icon bundled inside a package, resolved via ``importlib.resources``.
      Its bytes are copied into app-data (keyed by content hash) so pywebview
      always gets a stable real path, even from a zip-imported wheel. So a
      white-label package ships its own icon with no machine-specific path.

    Best-effort: a bad path / module / resource returns None (→ the caller falls
    back to the generated square)."""
    from .config import config_path

    v = value.strip()
    if v.lower().startswith("pkg:"):
        module, _, rel = v[4:].partition(":")
        module, rel = module.strip(), rel.strip()
        if not module or not rel:
            return None
        try:
            import hashlib
            import importlib.resources as resources

            res = resources.files(module)
            for part in rel.replace("\\", "/").split("/"):
                if part:
                    res = res / part
            if not res.is_file():
                return None
            data = res.read_bytes()
            suffix = Path(rel).suffix or ".ico"
            dest = (
                config_path().parent
                / f"runspec_console-icon-{hashlib.sha1(data).hexdigest()[:12]}{suffix}"
            )
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            return dest
        except Exception:
            return None

    p = Path(v).expanduser()
    return p if p.is_file() else None


def _build_icon() -> Path | None:
    """Generate a simple branded .ico, cached in app-data.

    A white-label install can override the icon outright with `[console.theme]
    icon` — a readable `.ico`/`.png` path, or a `pkg:<module>:<relpath>`
    reference to an icon bundled in a package (see `_resolve_icon_override`).
    Otherwise a solid 32×32 square is painted in `accent_color` (the brand
    colour) or runspec blue. The cache key includes the colour, so changing
    `accent_color` regenerates it."""
    import struct
    import zlib

    from .config import config_path, theme_config

    # An explicit icon (path or pkg: reference) wins — used verbatim.
    icon_override = theme_config().get("icon")
    if isinstance(icon_override, str) and icon_override.strip():
        p = _resolve_icon_override(icon_override)
        if p is not None:
            return p

    r, g, b = _icon_rgb()
    w = 32
    # Windows gets a real `.ico` (WebView2 taskbar icon); every other backend gets
    # a plain `.png`. pywebview's GTK backend hands the icon straight to
    # `Gtk.Window.set_icon_from_file()`, whose gdk-pixbuf ICO loader rejects a
    # PNG-compressed `.ico` ("Compressed icons are not supported") — and that
    # exception aborts window creation, leaving a blank 10×10 stub window with no
    # visible error. A `.png` loads cleanly on GTK and Qt alike. (Qt/WebKit are
    # equally happy with a PNG, so anything non-Windows takes this path.)
    is_windows = sys.platform == "win32"
    ext = "ico" if is_windows else "png"
    # `-rgba` in the name forces a regenerate over any stale pre-0.155.1 cache,
    # which declared 32bpp but embedded a 24-bit RGB PNG (no alpha) — Windows
    # filled the missing alpha with garbage, rendering the square with a dark-grey
    # artifact. The fix below emits a real opaque RGBA PNG.
    cache = config_path().parent / f"runspec_console-{r:02x}{g:02x}{b:02x}-rgba.{ext}"
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

        # 32-bit RGBA, every pixel fully opaque (alpha 255). Colour type 6 matches
        # the ICO dir entry's declared 32bpp, so there's a real alpha channel
        # instead of one Windows has to invent.
        scanlines = b"".join(b"\x00" + bytes([r, g, b, 255] * w) for _ in range(w))
        png = (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, w, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(scanlines))
            + _chunk(b"IEND", b"")
        )
        if is_windows:
            payload = (
                struct.pack("<HHH", 0, 1, 1)
                + struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(png), 22)
                + png
            )
        else:
            # A bare PNG — gdk-pixbuf / Qt load it directly, no ICO container.
            payload = png
        cache.write_bytes(payload)
        return cache
    except Exception:
        return None


def _apply_dwm_title_bar(title: str) -> None:
    """Style the native Windows title bar: dark mode + caption colour.

    The caption colour is the white-label `[console.theme] title_bar_color`
    when set — an **independent** hex, deliberately separate from `accent_color`
    so a brand can pair e.g. a blue accent UI with a red title bar — else
    runspec blue. The caption text colour follows the caption's luminance
    (`config.title_bar_colors`). The 1px window border DWM draws around the
    frame is tinted to the same caption colour: left at its default it stays
    near-white on a light Windows theme and shows as pale edge lines beside
    the coloured caption (re-drawn in a different shade on focus/resize, so it
    appears to come and go).

    Called from the webview `started` callback so the window HWND is available.
    Requires Windows 11 build 22000+ for DWMWA_CAPTION_COLOR (attr 35).
    """
    import ctypes
    import time

    try:
        import win32gui
    except ImportError:
        return

    from .config import title_bar_colors

    # COLORREF is 0x00BBGGRR. Runspec blue #0958D9 → 0x00D95809, white text.
    caption, text = title_bar_colors() or (0x00D95809, 0x00FFFFFF)

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

        # DWMWA_CAPTION_COLOR = 35
        cap = ctypes.c_int(caption)
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), ctypes.sizeof(cap))

        # DWMWA_TEXT_COLOR = 36
        txt = ctypes.c_int(text)
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), ctypes.sizeof(txt))

        # DWMWA_BORDER_COLOR = 34 — the thin frame around the window. Match the
        # caption so the title bar reads as one solid band edge to edge.
        border = ctypes.c_int(caption)
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
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


def _window_kwargs() -> dict[str, object]:
    """Static keyword args for :func:`webview.create_window`.

    Note ``private_mode`` is deliberately NOT here — it's a parameter of
    :func:`webview.start`, not ``create_window`` (passing it to ``create_window``
    raises ``TypeError``). See :func:`_start_kwargs`.
    """
    return {
        "width": 1440,
        "height": 900,
        "min_size": (1024, 600),
        # Paint the window the dark-theme content colour (App.tsx contentBg) so
        # there's no white flash before the SPA mounts.
        "background_color": "#101216",  # = ThemeContext.surfaces(dark).base — the pre-paint colour
        # Native "are you sure?" guard so an accidental close doesn't drop an
        # in-progress agent run or chat.
        "confirm_close": True,
        # Let users select & copy arbitrary page text (not just inputs/outputs).
        "text_select": True,
        # Ctrl+scroll / pinch to zoom the whole UI (readability / accessibility).
        "zoomable": True,
    }


def _start_kwargs(
    *, debug: bool, on_started: Any, icon: str | None
) -> dict[str, object]:
    """Keyword args for :func:`webview.start`.

    ``private_mode=False`` lives here (a ``start`` parameter, alongside ``icon``):
    pywebview defaults ``private_mode=True``, which on the GTK/WebKit backend flips
    ``enable_html5_local_storage`` off, so ``window.localStorage`` is *null* and the
    frontend crashes on its first ``getItem()`` before React mounts (a silent black
    screen on Linux). Keeping it false also matches intent — the console persists UI
    prefs (collapsed groups, per-runnable credential picks, …) across restarts.
    """
    kwargs: dict[str, object] = {
        "debug": debug,
        "func": on_started,
        # See the docstring — must stay False so localStorage works on WebKitGTK.
        "private_mode": False,
    }
    if icon:
        kwargs["icon"] = icon
    return kwargs


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

    # Pin an explicit config directory before anything reads config (the Bridge
    # loads it on construction). --config-dir wins over RUNSPEC_CONSOLE_DIR /
    # APPDATA so a test instance keeps its state isolated from the real profile.
    if args.config_dir.value is not None:
        from . import config as _cfg

        _cfg.set_app_dir(args.config_dir.value)
        logger.info("Using config directory: %s", _cfg.app_dir())

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

    # Bring up OpenTelemetry (OTLP) agent tracing if [telemetry] enabled — a
    # best-effort, idempotent no-op when disabled or the `otel` extra is absent.
    # One in-process init covers chat, trigger, and schedule turns.
    from . import config as _config
    from . import observability

    # With the AI off-switch set ([console] ai = false) none of the agent-only
    # bring-up below runs: tracing only traces agent turns, external MCP servers
    # only feed the agent's toolset, and the sandbox only serves agent tools.
    # The console's *own* MCP server (further down) stays — it gates an external
    # agent's fleet calls with no model inside.
    ai_on = bridge.ai_enabled()
    # Resolve any keychain-backed credential into the export headers first (traces
    # + metrics), so a per-user API key set up during onboarding authenticates the
    # export; observability itself stays keyring-free.
    _telemetry_cfg = bridge._resolve_telemetry_credentials()
    # Metrics cover *runnable* runs (Forms / MCP server / schedules / triggers via
    # invoke_runnable), not just agent activity, so they init **regardless of the
    # AI switch** — an AI-off fleet console still reports its run counts/durations.
    # Best-effort no-op when [telemetry.metrics] is disabled or the `otel` extra is
    # absent.
    observability.init_metrics(_telemetry_cfg)
    if ai_on:
        # Tracing is agent-turn-only (turn → LLM call → tool call), so it stays
        # gated on the AI switch — there are no agent turns to trace when AI is off.
        observability.init_tracing(_telemetry_cfg)

    # Connect any external MCP servers ([mcp] / [[mcp.servers]]) so their tools
    # join the chat agent's toolset. Best-effort and idempotent — a no-op unless
    # servers are configured and the `mcp` extra is installed; a slow/down server
    # just lands disconnected (visible in mcp_status) and never blocks startup.
    if ai_on:
        try:
            bridge.mcp_start()
        except Exception:  # noqa: BLE001
            pass

    # Start the console's own MCP server ([mcp_server]) so an external coding
    # agent (Claude Code) can reach the fleet over localhost. Best-effort and
    # off by default — a no-op unless [mcp_server] enabled and the `mcp` extra is
    # installed; it opens a listening socket, so it stays opt-in.
    try:
        bridge.mcp_server_start()
    except Exception:  # noqa: BLE001
        pass

    # Warm the WASM code-sandbox's compiled-module cache in the background so the
    # first run_python_sandboxed call isn't slow. Best-effort and download-free:
    # only compiles when the runtime + binary are already present locally (a
    # missing binary is left to lazy download-on-first-use behind the confirm gate).
    try:
        from . import sandbox

        def _warm_sandbox() -> None:
            try:
                sandbox.ensure_cwasm()
            except Exception:  # noqa: BLE001 — warming is best-effort
                pass

        if (
            ai_on
            and _config.read_workspace().get("code_exec", True)
            and _config.read_sandbox().get("enabled", True)
            and sandbox._wasmtime() is not None
            and sandbox.wasm_path() is not None
        ):
            threading.Thread(target=_warm_sandbox, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass

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
        **_window_kwargs(),
    )
    bridge.set_window(window)

    icon = _build_icon()
    start_kwargs = _start_kwargs(
        debug=webview_debug,
        on_started=lambda: _on_started(window_title),
        icon=str(icon) if icon else None,
    )
    try:
        webview.start(**start_kwargs)
    finally:
        # Flush any batched spans + metrics before the process exits (no-op when
        # off) so a short session doesn't drop its last traces / metric export.
        observability.shutdown()
        observability.shutdown_metrics()


def _crash_log_path() -> str:
    """Where a startup-crash traceback is written — the brand-aware config dir if
    reachable, else the OS temp dir. Best-effort; the caller tolerates either."""
    import tempfile

    try:
        from . import config

        base = str(config.app_dir())
    except Exception:  # noqa: BLE001 — the crash itself may be in config
        base = tempfile.gettempdir()
    return os.path.join(base, "startup-crash.log")


def _startup_title() -> str:
    """The app name for the crash dialog — branded when resolvable, else default."""
    try:
        from .branding import resolve_branding

        return resolve_branding().title
    except Exception:  # noqa: BLE001
        return "runspec console"


def _show_startup_error(summary: str, log_path: str | None) -> None:
    """Surface a startup crash to the user. Under a windowed (``pythonw``) launch
    there is no terminal, so show a native Windows dialog; elsewhere fall back to
    stderr. Overridable in tests. Never raises."""
    if sys.platform == "win32":
        try:
            import ctypes

            title = _startup_title()
            where = f"\n\nDetails written to:\n{log_path}" if log_path else ""
            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None, f"{title} could not start.\n\n{summary}{where}", title, 0x10
            )  # 0x10 = MB_ICONERROR
            return
        except Exception:  # noqa: BLE001
            pass
    try:
        print(summary, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def main_gui() -> None:
    """Windowed entry point — the ``gui-scripts`` launcher, which runs under
    ``pythonw.exe`` on Windows with **no console window**. It wraps :func:`main`
    so a crash *before* the app's own logging is up is not a silent no-op: the
    traceback is written to a crash log and shown in a native dialog, since there
    is no terminal to print it to. The plain ``runspec-console`` console script
    keeps printing tracebacks to its terminal, so this guard is windowed-only."""
    try:
        main()
    except Exception:  # noqa: BLE001 — last-resort GUI-launch startup guard
        import traceback

        tb = traceback.format_exc()
        path: str | None = _crash_log_path()
        try:
            from datetime import datetime

            with open(path, "a", encoding="utf-8") as fh:  # type: ignore[arg-type]
                fh.write(
                    f"\n--- startup crash {datetime.now().isoformat()} ---\n{tb}\n"
                )
        except Exception:  # noqa: BLE001
            path = None
        # The dialog shows the concise exception line; the full trace is in the file.
        lines = tb.strip().splitlines()
        _show_startup_error(lines[-1] if lines else "Unknown error.", path)
        raise SystemExit(1)

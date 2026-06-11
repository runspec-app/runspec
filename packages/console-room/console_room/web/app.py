"""
web/app.py — the Starlette app: the browser WebSocket door + the static frontend,
optionally gated by OIDC login.

Thin by design. It adapts a Starlette ``WebSocket`` to the ``WebSocketLike`` seam
``BrowserConnection`` expects, serves the vanilla static frontend, and exposes a
health check. When an ``ndjson_addr`` is given, the app's lifespan also runs the
console NDJSON listener on the same event loop, so one process serves both front
doors.

**Auth (S3).** With an :class:`AuthConfig`, the app mounts ``SessionMiddleware``
and the OIDC routes (``/login`` → IdP, ``/auth/callback`` → session, ``/logout``),
and gates the app shell + the WebSocket on the session identity — the browser no
longer declares who it is. Without one, the app runs in **dev mode**: identity
comes from the ``?user=`` query param and nothing is gated (what keeps local dev
and the tests runnable without an IdP). See docs/design/console-room-server.md
§1/§2/§6.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..hub import Hub
from ..ndjson_server import serve_ndjson
from .auth import AuthConfig, build_oauth, identity_from_userinfo, session_user
from .connection import BrowserConnection

logger = logging.getLogger("console_room.web")

FRONTEND_DIR = Path(__file__).parent / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"


class _StarletteWS:
    """Adapt a Starlette ``WebSocket`` to the ``WebSocketLike`` seam: ``receive``
    returns the next JSON object, ``{}`` for a non-JSON frame to skip, or ``None``
    once the client disconnects."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send_json(self, obj: dict[str, Any]) -> None:
        await self._ws.send_text(json.dumps(obj))

    async def receive(self) -> dict[str, Any] | None:
        try:
            text = await self._ws.receive_text()
        except WebSocketDisconnect:
            return None
        except RuntimeError:
            # Starlette raises this if the socket already moved to disconnected
            # (e.g. a client that vanished mid-teardown) — treat as a clean close.
            return None
        try:
            obj = json.loads(text)
        except ValueError:
            return {}  # skip a non-JSON frame, keep the socket open
        return obj if isinstance(obj, dict) else {}

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception:
            pass


def create_app(
    hub: Hub,
    *,
    ndjson_addr: tuple[str, int] | None = None,
    auth: AuthConfig | None = None,
) -> Starlette:
    """Build the Starlette app around ``hub``. With ``ndjson_addr=(host, port)``
    the lifespan also serves the console NDJSON door on the same loop. With
    ``auth`` the browser door requires an OIDC login; without it, dev mode."""

    oauth = build_oauth(auth) if auth is not None else None

    # ── WebSocket door ────────────────────────────────────────────────────────
    async def ws_room(websocket: WebSocket) -> None:
        room = (websocket.query_params.get("room") or "").strip()
        if auth is not None:
            user = session_user(websocket)
            if not user:
                await websocket.close(code=4401)  # unauthenticated
                return
        else:
            user = (websocket.query_params.get("user") or "").strip() or "guest"
        if not room:
            await websocket.close(code=4000)  # room required
            return
        await websocket.accept()
        await BrowserConnection(_StarletteWS(websocket), user=user, room=room).run(hub)

    # ── app shell + identity ──────────────────────────────────────────────────
    async def index(request: Any) -> Any:
        if auth is not None and not session_user(request):
            return RedirectResponse(url="/login", status_code=307)
        return FileResponse(INDEX_HTML)

    async def me(request: Any) -> JSONResponse:
        return JSONResponse({"auth": auth is not None, "user": session_user(request)})

    async def healthz(_request: Any) -> JSONResponse:
        return JSONResponse({"ok": True})

    # ── OIDC routes (auth mode only) ──────────────────────────────────────────
    async def login(request: Any) -> Any:
        assert auth is not None and oauth is not None
        redirect_uri = auth.redirect_uri(str(request.base_url))
        return await oauth.oidc.authorize_redirect(request, redirect_uri)

    async def auth_callback(request: Any) -> Any:
        assert oauth is not None
        try:
            token = await oauth.oidc.authorize_access_token(request)
        except Exception as exc:  # noqa: BLE001 — surface a clean 401, don't 500
            logger.warning("room: OIDC callback failed: %s", exc)
            return PlainTextResponse("login failed", status_code=401)
        user = identity_from_userinfo(token.get("userinfo"))
        if not user:
            return PlainTextResponse("login failed: no identity claim", status_code=401)
        request.session["user"] = user
        logger.info("room: %r logged in", user)
        return RedirectResponse(url="/", status_code=303)

    async def logout(request: Any) -> Any:
        request.session.pop("user", None)
        return RedirectResponse(url="/", status_code=303)

    # ── lifespan: also run the console NDJSON door ────────────────────────────
    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        server = None
        if ndjson_addr is not None:
            server = await serve_ndjson(hub, ndjson_addr[0], ndjson_addr[1])
        try:
            yield
        finally:
            if server is not None:
                server.close()
                await server.wait_closed()

    routes: list[Any] = [
        WebSocketRoute("/ws", ws_room, name="ws"),
        Route("/healthz", healthz, name="healthz"),
        Route("/me", me, name="me"),
        Route("/", index, name="index"),
    ]
    if auth is not None:
        routes += [
            Route("/login", login, name="login"),
            Route("/auth/callback", auth_callback, name="auth_callback"),
            Route("/logout", logout, name="logout"),
        ]
    if FRONTEND_DIR.is_dir():
        # Static assets (app.js / style.css) — public so the shell can style the
        # login redirect. The app shell itself ("/") is gated above.
        routes.append(
            Mount(
                "/static", app=StaticFiles(directory=str(FRONTEND_DIR)), name="static"
            )
        )

    middleware = []
    if auth is not None:
        middleware.append(
            Middleware(
                SessionMiddleware,
                secret_key=auth.session_secret,
                https_only=auth.secure_cookies,
                same_site="lax",
            )
        )

    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.oauth = oauth  # exposed so tests can stub the token exchange
    return app

"""
web/app.py — the Starlette app: the browser WebSocket door + the static frontend,
optionally gated by the email-verify login door.

Thin by design. It adapts a Starlette ``WebSocket`` to the ``WebSocketLike`` seam
``BrowserConnection`` expects, serves the vanilla static frontend, and exposes a
health check. When an ``ndjson_addr`` is given, the app's lifespan also runs the
console NDJSON listener on the same event loop, so one process serves both front
doors.

**Auth.** Two identity modes, sharing one ``session_user`` gate on the app shell +
WebSocket. With an :class:`EmailVerifyConfig` (+ an
:class:`~console_room.enrollment_store.EnrollmentStore`) the app mounts the
email-verify login door (``/login`` / ``/verify``) — an internal employee verifies
a company email to log into a queue (docs/design/email-queue-signup.md); the signed
session ``SessionMiddleware`` holds then **expires** (``session_ttl``), so access is
renewed by logging in again. With neither, the app runs in
**dev mode**: identity comes from the ``?user=`` query param and nothing is gated
(what keeps local dev and the tests runnable). See
docs/design/console-room-server.md §1/§2/§6.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..conversation import (
    DEFAULT_QUEUE,
    conversation_room,
    queue_namespace,
    valid_token,
)
from ..hub import Hub
from ..ndjson_server import serve_ndjson
from ..signup import EmailVerifyConfig
from .auth import session_name, session_team, session_user
from .connection import BrowserConnection
from .signup_routes import login_routes

logger = logging.getLogger("console_room.web")

FRONTEND_DIR = Path(__file__).parent / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"


def _ws_diag(websocket: WebSocket) -> str:
    """A one-line summary of a WebSocket handshake, for diagnosing a reject.

    A reject closes the socket *before* ``accept()``, which the ASGI server
    turns into an **HTTP 403** at the browser — the custom 4401/4000 close code
    never reaches it. So the only way to tell *why* a browser saw a 403 is the
    server log; this gives it the tells. The cookie fields are the key one for
    the common "login works over HTTP but the WS 403s" case: a reverse proxy
    that forwards ``Cookie`` on normal requests but drops it on the ``Upgrade``
    request leaves the WS with no session (``cookie-header=no``)."""
    client = websocket.client.host if websocket.client else "?"
    origin = websocket.headers.get("origin") or "-"
    cookie_header = "yes" if websocket.headers.get("cookie") else "no"
    session_cookie = "yes" if "session" in websocket.cookies else "no"
    return (
        f"client={client} origin={origin} "
        f"cookie-header={cookie_header} session-cookie={session_cookie}"
    )


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
    email_verify: EmailVerifyConfig | None = None,
) -> Starlette:
    """Build the Starlette app around ``hub``. With ``ndjson_addr=(host, port)``
    the lifespan also serves the console NDJSON door on the same loop.

    Identity comes from one of two modes: ``email_verify`` → the email-verify
    login door (``/login`` / ``/verify``), which requires ``hub`` to have been
    built with its enrollment store + config (``hub.email_verify_enabled``) and a
    ``public_url`` (the verify links must point somewhere reachable); neither → dev
    mode (``?user=`` query param, nothing gated). The authenticated mode sets a
    signed session cookie and gates the app shell + WebSocket on it."""
    if email_verify is not None:
        if not hub.email_verify_enabled:
            raise ValueError(
                "email_verify mode requires a Hub built with enrollment + email_verify"
            )
        if not email_verify.public_url:
            raise ValueError(
                "email_verify mode requires public_url — the verify links emailed to "
                "users must point at the room's externally-reachable base URL"
            )

    gated = email_verify is not None
    session_secret = email_verify.session_secret if email_verify is not None else ""
    secure_cookies = email_verify.secure_cookies if email_verify is not None else False
    # Access expires after this many seconds — the signed session cookie's max_age,
    # enforced server-side by itsdangerous. Past it the gate sees no session and
    # sends the user back to log in (a fresh email link).
    session_ttl = email_verify.session_ttl if email_verify is not None else 0
    # Where an unauthenticated / expired visitor is sent (email-verify mode).
    login_path = "/login"

    # ── WebSocket door ────────────────────────────────────────────────────────
    async def ws_room(websocket: WebSocket) -> None:
        if gated:
            user = session_user(websocket)
            if not user:
                logger.warning(
                    "room: WS rejected (close 4401 → browser sees HTTP 403) — "
                    "an authenticated session is required but none rode the "
                    "handshake. %s. cookie-header=no → a proxy is stripping "
                    "cookies on the WebSocket upgrade; cookie-header=yes but "
                    "session-cookie=no → the session cookie isn't reaching the "
                    "WS (name / path / Secure-over-plain-ws mismatch).",
                    _ws_diag(websocket),
                )
                await websocket.close(code=4401)  # unauthenticated
                return
        else:
            user = (websocket.query_params.get("user") or "").strip() or "guest"

        # Two visitor shapes. A `queue` param means a helpdesk visitor: the room is
        # derived server-side from the queue + the (authenticated) user, so a
        # visitor can never name — and thus never read — another visitor's thread.
        # Otherwise it's a group-chat browser naming its room directly (?room=).
        queue = (websocket.query_params.get("queue") or "").strip()
        if queue:
            if not valid_token(queue) or not valid_token(user):
                logger.warning(
                    "room: WS rejected (close 4000 → browser sees HTTP 403) — "
                    "unusable helpdesk token (queue=%r user=%r): a path "
                    "separator or whitespace is not allowed in either. %s",
                    queue,
                    user,
                    _ws_diag(websocket),
                )
                await websocket.close(code=4000)  # unusable queue / identity token
                return
            room = conversation_room(queue, user)
        else:
            room = (websocket.query_params.get("room") or "").strip()
            if not room:
                logger.info(
                    "room: WS rejected (close 4000 → browser sees HTTP 403) — "
                    "no ?room= or ?queue= in the handshake query. %s",
                    _ws_diag(websocket),
                )
                await websocket.close(code=4000)  # room required
                return
        logger.debug("room: WS accepted — user=%r room=%r", user, room)
        await websocket.accept()
        # The display name + team ride onto the operator card. In a verified server
        # they come from the signed session (authoritative — a query param can't
        # spoof them); in dev mode the browser declares the team via ?team= (the
        # name is the ?user= id), mirroring the verified experience.
        display_name = session_name(websocket) if gated else ""
        team = (
            session_team(websocket)
            if gated
            else (websocket.query_params.get("team") or "").strip()
        )
        await BrowserConnection(
            _StarletteWS(websocket),
            user=user,
            room=room,
            display_name=display_name,
            team=team,
        ).run(hub)

    # ── app shell + identity ──────────────────────────────────────────────────
    async def index(request: Any) -> Any:
        if gated and not session_user(request):
            return RedirectResponse(url=login_path, status_code=307)
        return FileResponse(INDEX_HTML)

    async def me(request: Any) -> JSONResponse:
        """Who the frontend is and where it joins. In a verified (email-verify)
        server the name + team come from the signed session; in dev mode they're
        blank here and the browser declares them (``?user=`` / ``?team=``). ``queue``
        is the single helpdesk queue this room serves — there's nothing to pick."""
        return JSONResponse(
            {
                "auth": gated,
                "user": session_user(request),
                "name": session_name(request) if gated else "",
                "team": session_team(request) if gated else "",
                "queue": DEFAULT_QUEUE,
            }
        )

    async def catalog(request: Any) -> JSONResponse:
        """The live self-service catalogue for a queue — the union of the runnables
        the *on-shift* agent consoles have published. Gated like the WS: in an
        authenticated mode you must be signed in."""
        if gated and not session_user(request):
            return JSONResponse({"error": "unauthenticated"}, status_code=401)
        queue = (request.query_params.get("queue") or "").strip()
        if not valid_token(queue):
            return JSONResponse({"error": "queue required"}, status_code=400)
        return JSONResponse(
            {"queue": queue, "tools": hub.live_catalog(queue_namespace(queue))}
        )

    async def healthz(_request: Any) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def favicon(_request: Any) -> Response:
        return Response(status_code=204)  # no icon — silence the browser's 404

    # ── lifespan: also run the console NDJSON door ────────────────────────────
    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        server = None
        if ndjson_addr is not None:
            server = await serve_ndjson(hub, ndjson_addr[0], ndjson_addr[1])
        # In email-verify mode, periodically drop expired pending signups (no-op
        # otherwise). Cancelled on shutdown.
        sweeper = (
            asyncio.create_task(hub.run_signup_sweeper())
            if email_verify is not None
            else None
        )
        try:
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
                with suppress(asyncio.CancelledError):
                    await sweeper
            if server is not None:
                server.close()
                await server.wait_closed()

    routes: list[Any] = [
        WebSocketRoute("/ws", ws_room, name="ws"),
        Route("/healthz", healthz, name="healthz"),
        Route("/favicon.ico", favicon, name="favicon"),
        Route("/me", me, name="me"),
        Route("/catalog", catalog, name="catalog"),
        Route("/", index, name="index"),
    ]
    if email_verify is not None:
        routes += login_routes(hub, email_verify)
    if FRONTEND_DIR.is_dir():
        # Static assets (app.js / style.css) — public so the shell can style the
        # login redirect. The app shell itself ("/") is gated above.
        routes.append(
            Mount(
                "/static", app=StaticFiles(directory=str(FRONTEND_DIR)), name="static"
            )
        )

    middleware = []
    if gated:
        middleware.append(
            Middleware(
                SessionMiddleware,
                secret_key=session_secret,
                max_age=session_ttl,  # access expiry → re-login via email link
                https_only=secure_cookies,
                same_site="lax",
            )
        )

    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)

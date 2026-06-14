"""
server.py — compose the store + hub + front doors and run them.

Default mode runs the **web server** (uvicorn + the Starlette app): browsers reach
the WebSocket door, and the app's lifespan also starts the console NDJSON door on
its own localhost port, so one process serves both. ``--ndjson-only`` runs just
the S1 console door on the stdlib asyncio path (no web dependencies) — handy for a
minimal deploy or an environment without Starlette/uvicorn.

OIDC + TLS (S3) layer onto the web mode later. Config is env-first with CLI
overrides. See docs/design/console-room-server.md §5/§6.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .hub import Hub
from .ndjson_server import serve_ndjson
from .store import TranscriptStore

logger = logging.getLogger("console_room.server")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8080
DEFAULT_NDJSON_PORT = 8765
DEFAULT_DB = "console_room.db"
DEFAULT_ASSIGNMENT_TIMEOUT = 300.0


async def run_ndjson_only(
    host: str, port: int, db_path: str, *, assignment_timeout: float = 300.0
) -> None:
    """Serve only the console NDJSON door (S1 path, stdlib only)."""
    store = TranscriptStore(db_path)
    hub = Hub(store, assignment_timeout=assignment_timeout)
    server = await serve_ndjson(hub, host, port)
    try:
        async with server:
            await server.serve_forever()
    finally:
        store.close()


def run_web(
    host: str,
    port: int,
    db_path: str,
    *,
    ndjson_host: str,
    ndjson_port: int,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    assignment_timeout: float = 300.0,
) -> None:
    """Serve the browser WebSocket door (and, via the app lifespan, the console
    NDJSON door) under uvicorn. Imports the web stack lazily so ``--ndjson-only``
    works without it installed. OIDC login is enabled when ``CONSOLE_ROOM_OIDC_*``
    is set; TLS when a cert + key are given (uvicorn is proxy-header-aware either
    way, so it also works behind a TLS-terminating reverse proxy)."""
    import uvicorn

    from .web.app import create_app
    from .web.auth import auth_from_env

    auth = auth_from_env()
    if auth is None:
        logger.warning(
            "room: OIDC not configured (CONSOLE_ROOM_OIDC_*) — running the browser "
            "door in DEV mode with NO login. Do not expose this publicly."
        )
    store = TranscriptStore(db_path)
    hub = Hub(store, assignment_timeout=assignment_timeout)
    app = create_app(hub, ndjson_addr=(ndjson_host, ndjson_port), auth=auth)
    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="info",
            ssl_certfile=tls_cert,
            ssl_keyfile=tls_key,
            proxy_headers=True,
            forwarded_allow_ips="*",
        )
    finally:
        store.close()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="console-room",
        description="Run the runspec console chat-room server.",
    )
    parser.add_argument(
        "--host",
        default=_env("CONSOLE_ROOM_HOST", DEFAULT_HOST),
        help="bind address for the browser web server (default: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(_env("CONSOLE_ROOM_PORT", str(DEFAULT_WEB_PORT))),
        help=f"port for the browser web server (default: {DEFAULT_WEB_PORT})",
    )
    parser.add_argument(
        "--ndjson-host",
        default=_env("CONSOLE_ROOM_NDJSON_HOST", DEFAULT_HOST),
        help="bind address for the console NDJSON door (default: localhost — "
        "consoles reach it over the SSH tunnel)",
    )
    parser.add_argument(
        "--ndjson-port",
        type=int,
        default=int(_env("CONSOLE_ROOM_NDJSON_PORT", str(DEFAULT_NDJSON_PORT))),
        help=f"port for the console NDJSON door (default: {DEFAULT_NDJSON_PORT})",
    )
    parser.add_argument(
        "--db",
        default=_env("CONSOLE_ROOM_DB", DEFAULT_DB),
        help=f"SQLite transcript path (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--assignment-timeout",
        type=float,
        default=float(
            _env("CONSOLE_ROOM_ASSIGNMENT_TIMEOUT", str(DEFAULT_ASSIGNMENT_TIMEOUT))
        ),
        help="seconds an assigned request may sit before a console picks it up "
        "(sends request_start) — then it's auto-released to the queue and re-paged. "
        f"0 disables. (default: {DEFAULT_ASSIGNMENT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--tls-cert",
        default=_env("CONSOLE_ROOM_TLS_CERT", "") or None,
        help="path to the TLS certificate (PEM) — enables https/wss. Omit if a "
        "reverse proxy terminates TLS.",
    )
    parser.add_argument(
        "--tls-key",
        default=_env("CONSOLE_ROOM_TLS_KEY", "") or None,
        help="path to the TLS private key (PEM)",
    )
    parser.add_argument(
        "--ndjson-only",
        action="store_true",
        help="serve only the console NDJSON door (no web deps required)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    # `console-room connect …` is the exec-transport relay (stdio↔local room port).
    # It must keep stdout pure (it's the data channel), so it configures no logging.
    if raw and raw[0] == "connect":
        from .connect import main as connect_main

        raise SystemExit(connect_main(raw[1:]))

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = _parse_args(raw)
    try:
        if args.ndjson_only:
            asyncio.run(
                run_ndjson_only(
                    args.ndjson_host,
                    args.ndjson_port,
                    args.db,
                    assignment_timeout=args.assignment_timeout,
                )
            )
        else:
            run_web(
                args.host,
                args.port,
                args.db,
                ndjson_host=args.ndjson_host,
                ndjson_port=args.ndjson_port,
                tls_cert=args.tls_cert,
                tls_key=args.tls_key,
                assignment_timeout=args.assignment_timeout,
            )
    except KeyboardInterrupt:
        logger.info("room: shutting down")


if __name__ == "__main__":  # pragma: no cover
    main()

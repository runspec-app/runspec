"""
server.py — compose the store + hub + front doors and run them.

Default mode runs the **web server** (uvicorn + the Starlette app): browsers reach
the WebSocket door, and the app's lifespan also starts the console NDJSON door on
its own localhost port, so one process serves both. ``--ndjson-only`` runs just
the S1 console door on the stdlib asyncio path (no web dependencies) — handy for a
minimal deploy or an environment without Starlette/uvicorn.

TLS layers onto the web mode later. Config is env-first with CLI
overrides. See docs/design/console-room-server.md §5/§6.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .config import (
    apply_config_to_env,
    config_teams,
    find_config_path,
    load_config,
)
from .hub import Hub
from .ndjson_server import serve_ndjson
from .signup import Team
from .store import TranscriptStore

logger = logging.getLogger("console_room.server")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8080
DEFAULT_NDJSON_PORT = 8765
DEFAULT_DB = "console_room.db"
DEFAULT_ASSIGNMENT_TIMEOUT = 300.0


async def run_ndjson_only(
    host: str,
    port: int,
    db_path: str,
    *,
    assignment_timeout: float = 300.0,
    mailbox: str = "",
) -> None:
    """Serve only the console NDJSON door (S1 path, stdlib only)."""
    store = TranscriptStore(db_path)
    hub = Hub(store, assignment_timeout=assignment_timeout, mailbox=mailbox)
    server = await serve_ndjson(hub, host, port)
    try:
        async with server:
            await server.serve_forever()
    finally:
        store.close()


def _enroll_db_path(db_path: str) -> str:
    """Where the email-verify members/outbox live — a sibling of the transcript DB
    (``…/console_room.db`` → ``…/console_room.enrollment.db``), or ``:memory:`` when
    the transcript is in memory."""
    override = os.environ.get("CONSOLE_ROOM_ENROLL_DB")
    if override:
        return override
    if db_path == ":memory:":
        return ":memory:"
    p = Path(db_path)
    return str(p.with_name(f"{p.stem}.enrollment{p.suffix or '.db'}"))


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
    mailbox: str = "",
    teams: list[Team] | None = None,
) -> None:
    """Serve the browser WebSocket door (and, via the app lifespan, the console
    NDJSON door) under uvicorn. Imports the web stack lazily so ``--ndjson-only``
    works without it installed. The auth mode is ``CONSOLE_ROOM_AUTH``
    (``email-verify`` / ``dev``); it defaults to **email-verify** when a session
    secret is set, else dev — email sign-up is the only authenticated door. TLS when
    a cert + key are given (uvicorn is proxy-header-aware, so it also works behind a
    TLS-terminating reverse proxy)."""
    import uvicorn

    from .enrollment_store import EnrollmentStore
    from .signup import email_verify_from_env, read_auth_mode
    from .web.app import create_app

    mode = read_auth_mode()
    email_verify = None
    enrollment: EnrollmentStore | None = None
    if mode == "email-verify":
        email_verify = email_verify_from_env()
        if email_verify is None:
            raise SystemExit(
                "CONSOLE_ROOM_AUTH=email-verify requires CONSOLE_ROOM_SESSION_SECRET."
            )
        if not email_verify.public_url:
            # The verify link is emailed and clicked later, so it can't be guessed
            # from a request — it must be a stable, externally-reachable URL.
            raise SystemExit(
                "CONSOLE_ROOM_AUTH=email-verify requires a public URL — set "
                "CONSOLE_ROOM_PUBLIC_URL (or --public-url) to the room's "
                "externally-reachable base, e.g. https://rooms.corp.com. Verify "
                "links point there."
            )
        enrollment = EnrollmentStore(_enroll_db_path(db_path))
        if teams:
            # Config is the authoritative team list (synced on every boot) so a
            # fleet of rooms deploying the same file converges on one list.
            enrollment.sync_teams(teams)
            logger.info("room: synced %d team(s) from config", len(teams))
        logger.info(
            "room: email-verify login enabled (access expires after %ds) — the login "
            "mail is sent by a connected console as the queue mailbox (logins queue in "
            "the outbox until one connects). Login links use %s.",
            email_verify.session_ttl,
            email_verify.public_url,
        )
    if email_verify is None:
        logger.warning(
            "room: no auth configured — running the browser door in DEV mode with "
            "NO login. Do not expose this publicly."
        )

    store = TranscriptStore(db_path)
    hub = Hub(
        store,
        assignment_timeout=assignment_timeout,
        enrollment=enrollment,
        email_verify=email_verify,
        mailbox=mailbox,
    )
    app = create_app(
        hub,
        ndjson_addr=(ndjson_host, ndjson_port),
        email_verify=email_verify,
    )
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
        if enrollment is not None:
            enrollment.close()


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
        "--public-url",
        default=_env("CONSOLE_ROOM_PUBLIC_URL", ""),
        help="the room's externally-reachable base URL (e.g. https://rooms.corp.com)"
        " — required for CONSOLE_ROOM_AUTH=email-verify, where the emailed verify "
        "links must point at it.",
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
        "--mailbox",
        default=_env("CONSOLE_ROOM_MAILBOX", ""),
        help="the helpdesk mailbox this room serves (e.g. helpdesk@corp.com). When "
        "set, only consoles configured for the same [mailbox] may connect — one "
        "email = one room. Leave empty to accept any console (dev).",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("CONSOLE_ROOM_CONFIG", ""),
        help="path to a TOML config file (see docs) — also CONSOLE_ROOM_CONFIG. "
        "Settings load with precedence defaults < config < env var < CLI flag.",
    )
    parser.add_argument(
        "--ndjson-only",
        action="store_true",
        help="serve only the console NDJSON door (no web deps required)",
    )
    return parser.parse_args(argv)


def _load_config(argv: list[str]) -> list[Team]:
    """Find + load the TOML config (``--config`` / ``CONSOLE_ROOM_CONFIG`` /
    ``./console_room.toml``), apply its scalars to the environment (only where not
    already set, so env/CLI still win), and return its ``[[teams]]``. Run before
    arg parsing so the argparse env-defaults see the config-injected values."""
    cli_path = ""
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            cli_path = argv[i + 1]
        elif arg.startswith("--config="):
            cli_path = arg.split("=", 1)[1]
    config = load_config(find_config_path(cli_path or None))
    apply_config_to_env(config)
    return config_teams(config)


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
    # Load the optional config file first so its values populate the environment
    # the argparse defaults read from (config < env < CLI). Returns the team list.
    teams = _load_config(raw)
    args = _parse_args(raw)
    # --public-url is surfaced via the same env var the auth configs read, so the
    # flag and CONSOLE_ROOM_PUBLIC_URL stay a single source of truth.
    if args.public_url:
        os.environ["CONSOLE_ROOM_PUBLIC_URL"] = args.public_url
    try:
        if args.ndjson_only:
            asyncio.run(
                run_ndjson_only(
                    args.ndjson_host,
                    args.ndjson_port,
                    args.db,
                    assignment_timeout=args.assignment_timeout,
                    mailbox=args.mailbox,
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
                mailbox=args.mailbox,
                teams=teams,
            )
    except KeyboardInterrupt:
        logger.info("room: shutting down")


if __name__ == "__main__":  # pragma: no cover
    main()

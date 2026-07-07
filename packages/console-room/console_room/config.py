"""
config.py — optional TOML config file for the room server (deploy-from-one-file).

The room is normally configured by environment variables + CLI flags (server.py).
For an Ansible-style deploy that's awkward: a unit file with a dozen ``Environment=``
lines, and the session secret sitting in plaintext. This module adds a single TOML
file that supplies the same settings, so one templated file stands an instance up.

Layering (lowest → highest precedence): **defaults < config file < env var < CLI
flag**. The integration is deliberately low-churn: :func:`apply_config_to_env`
writes each setting into the matching ``CONSOLE_ROOM_*`` environment variable *only
when it isn't already set*, so every existing env read (in ``signup.py`` and the
argparse env-defaults in ``server.py``) keeps working unchanged and a real env var
or CLI flag always wins.

``[server]`` also accepts ``log_level`` (``debug``/``info``/``warning``/``error``)
and ``log_file`` (an explicit path; otherwise the server rotates
``{sys.prefix}/logs/room.log`` at midnight, keeping 7 days) — see ``server.py``.

Two things never round-trip through a plain string env var:

- the **session secret**, which can be a ``session_secret_command`` whose stdout is
  the secret (so it stays out of the committed file) — see
  :func:`resolve_session_secret`;
- the **teams** list, parsed by :func:`config_teams` and applied authoritatively to
  the enrollment store on boot (server.py), so a fleet of rooms shares one list.

**White-label branding** ``[branding]`` (mirrors the desktop console's
``[console.theme]`` key names) themes the browser app + the server-rendered login
pages with no adapter/plugin — see :class:`Branding` / :func:`branding_from_env`::

    [branding]
    brand_name   = "MyCorp Ops"          # browser tab + join heading + login title
    accent_color = "#b3001b"             # #rgb / #rrggbb — primary button/accent
    favicon      = "/srv/brand/app.ico"  # served at /favicon.ico

These are plain scalars, so they layer through :func:`apply_config_to_env` like the
``[server]``/``[auth]`` settings (``CONSOLE_ROOM_BRAND_NAME`` /
``CONSOLE_ROOM_ACCENT_COLOR`` / ``CONSOLE_ROOM_FAVICON``).

Stdlib only (``tomllib`` on 3.11+, ``tomli`` on 3.10; ``subprocess`` for the
command). No I/O on import; a missing / unreadable file is an empty config.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .signup import Team, make_team_slug, parse_domains, parse_emails

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

logger = logging.getLogger("console_room.config")

#: Config file looked for in the working directory when neither ``--config`` nor
#: ``CONSOLE_ROOM_CONFIG`` names one.
DEFAULT_CONFIG_NAME = "console_room.toml"

#: ``[server]`` / ``[auth]`` key → the ``CONSOLE_ROOM_*`` env var it feeds. Only the
#: scalar settings live here; the session secret, teams, and the allowed-domains
#: list (which may be a TOML array) are handled separately.
_ENV_MAP: dict[tuple[str, str], str] = {
    ("server", "host"): "CONSOLE_ROOM_HOST",
    ("server", "port"): "CONSOLE_ROOM_PORT",
    ("server", "ndjson_host"): "CONSOLE_ROOM_NDJSON_HOST",
    ("server", "ndjson_port"): "CONSOLE_ROOM_NDJSON_PORT",
    ("server", "public_url"): "CONSOLE_ROOM_PUBLIC_URL",
    ("server", "mailbox"): "CONSOLE_ROOM_MAILBOX",
    ("server", "db"): "CONSOLE_ROOM_DB",
    ("server", "tls_cert"): "CONSOLE_ROOM_TLS_CERT",
    ("server", "tls_key"): "CONSOLE_ROOM_TLS_KEY",
    ("server", "assignment_timeout"): "CONSOLE_ROOM_ASSIGNMENT_TIMEOUT",
    # Logging: verbosity + an optional explicit log-file path. Both layer through
    # env like the other scalars (config < env < CLI). With no log_file the server
    # defaults to a rotating {sys.prefix}/logs/room.log (see server.py).
    ("server", "log_level"): "CONSOLE_ROOM_LOG_LEVEL",
    ("server", "log_file"): "CONSOLE_ROOM_LOG_FILE",
    ("auth", "verify_ttl"): "CONSOLE_ROOM_VERIFY_TTL",
    ("auth", "session_ttl"): "CONSOLE_ROOM_SESSION_TTL",
    ("auth", "enroll_db"): "CONSOLE_ROOM_ENROLL_DB",
    # White-label the browser app + login pages (mirrors the desktop console's
    # `[console.theme]` keys, kept flat under `[branding]` for the room's config).
    ("branding", "brand_name"): "CONSOLE_ROOM_BRAND_NAME",
    ("branding", "accent_color"): "CONSOLE_ROOM_ACCENT_COLOR",
    ("branding", "favicon"): "CONSOLE_ROOM_FAVICON",
}


def find_config_path(cli_path: str | None) -> Path | None:
    """Resolve the config file: ``--config`` > ``CONSOLE_ROOM_CONFIG`` >
    ``./console_room.toml`` (only if it exists), else ``None`` (no config — pure
    env/CLI, the prior behaviour). An explicitly named path is returned even when
    absent, so a typo'd ``--config`` surfaces as a load warning rather than being
    silently ignored."""
    if cli_path:
        return Path(cli_path)
    env = (os.environ.get("CONSOLE_ROOM_CONFIG") or "").strip()
    if env:
        return Path(env)
    default = Path(DEFAULT_CONFIG_NAME)
    return default if default.is_file() else None


def load_config(path: Path | None) -> dict[str, Any]:
    """Parse the TOML config at ``path`` into a dict, or ``{}`` when there's no path
    or it can't be read/parsed (the room then runs on env/CLI alone). A present-but-
    broken file is logged, not fatal — a deploy mistake shouldn't wedge the boot
    before the clearer downstream errors (missing secret / public URL) fire."""
    if path is None:
        return {}
    if not path.is_file():
        logger.warning("room: config file %s not found — ignoring", path)
        return {}
    try:
        with open(path, "rb") as f:
            return dict(tomllib.load(f))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(
            "room: config file %s could not be read (%s) — ignoring", path, exc
        )
        return {}


def resolve_session_secret(config: dict[str, Any]) -> str | None:
    """The session secret from ``[auth]``: a literal ``session_secret``, or the
    stdout of ``session_secret_command`` (preferred — keeps the secret out of the
    committed file). ``None`` when neither is set (env/CLI may still supply it).
    The literal wins if both are given. A failing command raises ``SystemExit`` —
    a deploy that names a secret source but can't read it must not start with no
    auth."""
    auth = config.get("auth") or {}
    literal = str(auth.get("session_secret") or "").strip()
    if literal:
        return literal
    command = str(auth.get("session_secret_command") or "").strip()
    if not command:
        return None
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"session_secret_command failed (exit {exc.returncode}): "
            f"{(exc.stderr or '').strip()}"
        ) from exc
    secret = result.stdout.strip()
    if not secret:
        raise SystemExit("session_secret_command produced no output")
    return secret


def apply_config_to_env(config: dict[str, Any]) -> None:
    """Write each ``[server]``/``[auth]`` scalar into its ``CONSOLE_ROOM_*`` env var,
    **only when that var isn't already set** — so a real env var or a CLI flag (which
    the argparse defaults read from env) always overrides the file. The session
    secret is resolved (command or literal) and injected the same way."""
    for (section, key), env_name in _ENV_MAP.items():
        if env_name in os.environ:
            continue  # a real env var / CLI already won — never clobber it
        value = (config.get(section) or {}).get(key)
        if value is None or value == "":
            continue
        os.environ[env_name] = str(value)
    if "CONSOLE_ROOM_SESSION_SECRET" not in os.environ:
        secret = resolve_session_secret(config)
        if secret:
            os.environ["CONSOLE_ROOM_SESSION_SECRET"] = secret
    if "CONSOLE_ROOM_ALLOWED_DOMAINS" not in os.environ:
        domains = config_allowed_domains(config)
        if domains:
            os.environ["CONSOLE_ROOM_ALLOWED_DOMAINS"] = ",".join(domains)
    if "CONSOLE_ROOM_OPERATORS" not in os.environ:
        operators = config_operators(config)
        if operators:
            os.environ["CONSOLE_ROOM_OPERATORS"] = ",".join(operators)


def config_allowed_domains(config: dict[str, Any]) -> list[str]:
    """The extra login domains from ``[auth] allowed_domains`` — a TOML array
    (``["company1.com", "company2.com"]``) or a comma/space-separated string.
    Normalized (lowercased, leading ``@`` stripped) and deduped; ``[]`` when unset.
    These widen the login allowlist beyond the queue mailbox's own domain (always
    allowed) — for a company whose people sit on a few domain variants behind one
    monitored mailbox."""
    raw = (config.get("auth") or {}).get("allowed_domains")
    if raw is None:
        return []
    if isinstance(raw, str):
        return parse_domains(raw)
    if isinstance(raw, (list, tuple)):
        return parse_domains(",".join(str(item) for item in raw))
    return []


def config_operators(config: dict[str, Any]) -> list[str]:
    """The operator allowlist from ``[auth] operators`` — a TOML array
    (``["alice@corp.com", "bob@corp.com"]``) or a comma/space-separated string.
    Normalized (lowercased, deduped, ``local@domain`` only); ``[]`` when unset.
    These emails get the elevated operator room view (full catalogue + runbooks)."""
    raw = (config.get("auth") or {}).get("operators")
    if raw is None:
        return []
    if isinstance(raw, str):
        return parse_emails(raw)
    if isinstance(raw, (list, tuple)):
        return parse_emails(",".join(str(item) for item in raw))
    return []


_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def valid_hex_color(value: Any) -> str:
    """Return ``value`` if it's a hex colour (``#rgb`` / ``#rrggbb``), else ``""``.

    Mirrors the desktop console's ``valid_accent`` — a garbage value is dropped
    rather than injected raw into the page CSS."""
    if isinstance(value, str) and _HEX_COLOR_RE.match(value.strip()):
        return value.strip()
    return ""


@dataclass(frozen=True)
class Branding:
    """White-label look for the browser app + server-rendered login pages, from
    ``[branding]`` (mirrors the desktop console's ``[console.theme]`` keys).

    ``brand_name`` renames the browser tab + join heading (and prefixes the login
    page titles); ``accent_color`` recolours the primary button / accent; a
    ``favicon`` filesystem path is served at ``/favicon.ico``. Every field is
    optional — an empty ``Branding`` reproduces today's default runspec look."""

    brand_name: str = ""
    accent_color: str = ""
    favicon_path: str = ""

    def public(self) -> dict[str, str]:
        """The subset safe to hand the browser at ``/me`` — never the favicon
        *path* (a server filesystem detail; the bytes are served at
        ``/favicon.ico``). Empty keys are dropped so an unbranded server returns
        ``{}`` and the frontend keeps its defaults."""
        out: dict[str, str] = {}
        if self.brand_name:
            out["brand_name"] = self.brand_name
        if self.accent_color:
            out["accent_color"] = self.accent_color
        return out


def branding_from_env() -> Branding:
    """Build :class:`Branding` from the ``CONSOLE_ROOM_*`` env vars (which
    :func:`apply_config_to_env` has already populated from ``[branding]``). The
    accent colour is hex-validated; a bad value is dropped, not raised."""
    return Branding(
        brand_name=(os.environ.get("CONSOLE_ROOM_BRAND_NAME") or "").strip(),
        accent_color=valid_hex_color(os.environ.get("CONSOLE_ROOM_ACCENT_COLOR")),
        favicon_path=(os.environ.get("CONSOLE_ROOM_FAVICON") or "").strip(),
    )


def config_teams(config: dict[str, Any]) -> list[Team]:
    """The ``[[teams]]`` entries as :class:`~console_room.signup.Team` values. Each
    needs a ``name``; ``slug`` is taken as given or derived from the name. Entries
    with no usable slug are skipped. Returns ``[]`` when no teams are declared — the
    caller treats that as "don't touch the team list" (not "delete everything")."""
    teams: list[Team] = []
    for entry in config.get("teams") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        slug = str(entry.get("slug") or "").strip() or make_team_slug(name)
        if name and slug:
            teams.append(Team(slug=slug, name=name))
    return teams

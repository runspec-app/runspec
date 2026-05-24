import atexit
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import warnings
from pathlib import Path

import runspec as rs


def _shared_pass_key() -> str:
    return "SSH_PASS"


def _host_pass_key(host_name: str) -> str:
    return f"SSH_{host_name.upper().replace('-', '_').replace(' ', '_')}_PASS"


def _resolve_hosts_path(path: Path) -> Path:
    if not path.exists():
        legacy = path.parent / "hosts.toml"
        if legacy.exists():
            warnings.warn(
                f"[runspec-chat] {legacy} is deprecated; rename to {path.name} to suppress this warning.",
                DeprecationWarning,
                stacklevel=2,
            )
            return legacy
    return path


def _sync_user_env(hosts_path: Path, chainlit_config: Path) -> None:
    hosts_path = _resolve_hosts_path(hosts_path)
    user_env = ["ANTHROPIC_API_KEY"]

    if hosts_path.exists():
        with open(hosts_path, "rb") as f:
            cfg = tomllib.load(f)

        has_shared = False
        host_keys: list[str] = []
        for name, info in cfg.get("hosts", {}).items():
            if not info.get("ssh"):
                continue
            if info.get("user"):
                host_keys.append(_host_pass_key(name))
            else:
                has_shared = True

        if has_shared:
            user_env.append(_shared_pass_key())
        user_env.extend(host_keys)

    if not chainlit_config.exists():
        return

    text = chainlit_config.read_text()
    text = re.sub(r"user_env = \[.*?\]", f"user_env = {json.dumps(user_env)}", text)
    chainlit_config.write_text(text)


def _init_chainlit_root(hosts_path: Path) -> Path:
    src = Path(__file__).parent / "chainlit_config"
    tmp = Path(tempfile.mkdtemp(prefix="runspec-chat-"))
    atexit.register(shutil.rmtree, tmp, True)
    shutil.copytree(src, tmp / ".chainlit")
    shutil.copy(Path(__file__).parent / "chainlit.md", tmp / "chainlit.md")
    _sync_user_env(hosts_path, tmp / ".chainlit" / "config.toml")
    return tmp


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    "id"         TEXT PRIMARY KEY,
    "identifier" TEXT NOT NULL UNIQUE,
    "metadata"   TEXT NOT NULL,
    "createdAt"  TEXT
);
CREATE TABLE IF NOT EXISTS threads (
    "id"             TEXT PRIMARY KEY,
    "createdAt"      TEXT,
    "name"           TEXT,
    "userId"         TEXT,
    "userIdentifier" TEXT,
    "tags"           TEXT,
    "metadata"       TEXT,
    FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS steps (
    "id"            TEXT PRIMARY KEY,
    "name"          TEXT NOT NULL,
    "type"          TEXT NOT NULL,
    "threadId"      TEXT NOT NULL,
    "parentId"      TEXT,
    "streaming"     INTEGER NOT NULL,
    "waitForAnswer" INTEGER,
    "isError"       INTEGER,
    "metadata"      TEXT,
    "tags"          TEXT,
    "input"         TEXT,
    "output"        TEXT,
    "createdAt"     TEXT,
    "command"       TEXT,
    "start"         TEXT,
    "end"           TEXT,
    "generation"    TEXT,
    "showInput"     TEXT,
    "language"      TEXT,
    "indent"        INTEGER,
    "defaultOpen"   INTEGER,
    "autoCollapse"  INTEGER,
    "modes"         TEXT,
    "icon"          TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS elements (
    "id"          TEXT PRIMARY KEY,
    "threadId"    TEXT,
    "type"        TEXT,
    "url"         TEXT,
    "chainlitKey" TEXT,
    "name"        TEXT NOT NULL,
    "display"     TEXT,
    "objectKey"   TEXT,
    "size"        TEXT,
    "page"        INTEGER,
    "language"    TEXT,
    "forId"       TEXT,
    "mime"        TEXT,
    "props"       TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS feedbacks (
    "id"       TEXT PRIMARY KEY,
    "forId"    TEXT NOT NULL,
    "threadId" TEXT NOT NULL,
    "value"    INTEGER NOT NULL,
    "comment"  TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
"""


def _get_config_dir() -> Path:
    return Path("~/.config/runspec-chat").expanduser()


def _ensure_auth_secret() -> str:
    """Load or generate a persistent JWT secret for Chainlit auth."""
    secret_file = _get_config_dir() / "auth.secret"
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    if secret_file.exists():
        return secret_file.read_text().strip()
    secret = secrets.token_hex(32)
    secret_file.write_text(secret)
    return secret


def _ensure_db(db_path: Path) -> None:
    """Create the SQLite history DB schema and apply any missing column migrations."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(_SQLITE_SCHEMA)

    # Add any columns introduced after the initial schema (ALTER TABLE is
    # idempotent-guarded by checking existing columns first).
    _add_columns_if_missing(
        con,
        "steps",
        [
            ("autoCollapse", "INTEGER"),
            ("icon", "TEXT"),
        ],
    )

    con.commit()
    con.close()


def _add_columns_if_missing(
    con: sqlite3.Connection, table: str, columns: list[tuple[str, str]]
) -> None:
    existing = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
    for col_name, col_type in columns:
        if col_name not in existing:
            con.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col_name}" {col_type}')


def main() -> None:
    spec = rs.parse("runspec-chat")

    hosts_path = Path(str(spec.hosts)).expanduser()
    chainlit_root = _init_chainlit_root(hosts_path)
    os.environ["CHAINLIT_APP_ROOT"] = str(chainlit_root)

    # Auth secret — required by Chainlit when any auth callback is registered
    os.environ["CHAINLIT_AUTH_SECRET"] = _ensure_auth_secret()

    # History DB — create schema once before the server starts
    db_path = _get_config_dir() / "history.db"
    _ensure_db(db_path)
    os.environ["RUNSPEC_CHAT_DB"] = str(db_path)

    if spec.model:
        os.environ["RUNSPEC_CHAT_MODEL"] = str(spec.model)
    if spec.hosts:
        os.environ["RUNSPEC_CHAT_HOSTS"] = str(spec.hosts)

    app_py = Path(__file__).parent / "app.py"
    # Use our launcher instead of `python -m chainlit` so the Python 3.14
    # nest_asyncio compatibility shim is applied before chainlit's CLI runs.
    cmd = [
        sys.executable,
        "-m",
        "runspec_chat._chainlit_launcher",
        "run",
        str(app_py),
        "--port",
        str(spec.port),
        "--host",
        "127.0.0.1",
    ]
    if spec.watch:
        cmd.append("--watch")
    if spec.headless:
        cmd.append("--headless")
    if spec.root_path:
        cmd += ["--root-path", str(spec.root_path)]
    if spec.ssl_cert:
        cmd += ["--ssl-cert", str(spec.ssl_cert)]
    if spec.ssl_key:
        cmd += ["--ssl-key", str(spec.ssl_key)]
    try:
        sys.exit(subprocess.run(cmd).returncode)
    except KeyboardInterrupt:
        sys.exit(0)

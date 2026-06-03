"""auth.py — Microsoft Graph delegated auth via MSAL device-code flow.

Token cache persists to ``%APPDATA%/runspec-windows/msal_cache.bin``. The Azure
``client_id`` (a public client — no secret) and ``tenant`` come from the
environment or ``%APPDATA%/runspec-windows/graph.toml``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runspec_windows.graph import GraphAuthError, GraphConfigError, GraphDepError

try:
    import msal
except ImportError:  # optional [graph] dependency
    msal = None  # type: ignore[assignment]

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:  # tomllib is the only consumer; degrade gracefully
        tomllib = None  # type: ignore[assignment]

DEFAULT_SCOPES = ["User.Read", "Mail.Read", "Chat.Read", "Calendars.Read", "Files.Read.All"]
_AUTHORITY_BASE = "https://login.microsoftonline.com"


def _require_msal() -> Any:
    if msal is None:
        raise GraphDepError("The 'msal' package is required for Microsoft Graph runnables. Install with: pip install 'runspec-windows[graph]'")
    return msal


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / "runspec-windows"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path() -> Path:
    return config_dir() / "msal_cache.bin"


def _read_graph_toml() -> dict:
    path = config_dir() / "graph.toml"
    if not path.exists() or tomllib is None:
        return {}
    try:
        with open(path, "rb") as f:
            return dict(tomllib.load(f))
    except Exception:
        return {}


def client_id() -> str:
    cid = os.environ.get("RUNSPEC_GRAPH_CLIENT_ID") or _read_graph_toml().get("client_id")
    if not cid:
        raise GraphConfigError("No Azure client id. Set RUNSPEC_GRAPH_CLIENT_ID or add client_id to %APPDATA%/runspec-windows/graph.toml. See the README for app-registration steps.")
    return str(cid)


def tenant() -> str:
    return str(os.environ.get("RUNSPEC_GRAPH_TENANT") or _read_graph_toml().get("tenant") or "organizations")


def _load_cache() -> Any:
    m = _require_msal()
    cache = m.SerializableTokenCache()
    path = _cache_path()
    if path.exists():
        cache.deserialize(path.read_text(encoding="utf-8"))
    return cache


def _save_cache(cache: Any) -> None:
    if cache.has_state_changed:
        _cache_path().write_text(cache.serialize(), encoding="utf-8")


def _build_app(cache: Any) -> Any:
    m = _require_msal()
    return m.PublicClientApplication(client_id(), authority=f"{_AUTHORITY_BASE}/{tenant()}", token_cache=cache)


def get_token(scopes: list[str] | None = None) -> str:
    """Return a cached access token (silent refresh), or raise ``GraphAuthError``.

    Does not start an interactive flow — call ``device_login`` (the ``graph-login``
    runnable) once to populate the cache.
    """
    scopes = scopes or DEFAULT_SCOPES
    cache = _load_cache()
    app = _build_app(cache)
    accounts = app.get_accounts()
    result = app.acquire_token_silent(scopes, account=accounts[0]) if accounts else None
    _save_cache(cache)
    if not result or "access_token" not in result:
        raise GraphAuthError("Not signed in to Microsoft 365 — run 'graph-login' first.")
    return str(result["access_token"])


def device_login(scopes: list[str] | None = None, on_prompt: Callable[[str], None] | None = None) -> dict:
    """Run the device-code flow interactively and cache the resulting token.

    ``on_prompt`` receives the human-readable instructions (verification URL +
    code); defaults to printing them to stderr so stdout stays clean JSON.
    """
    scopes = scopes or DEFAULT_SCOPES
    cache = _load_cache()
    app = _build_app(cache)
    flow = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        raise GraphAuthError(f"Failed to start device-code flow: {flow.get('error_description', flow)}")
    (on_prompt or (lambda m: print(m, file=sys.stderr)))(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    _save_cache(cache)
    if "access_token" not in result:
        raise GraphAuthError(result.get("error_description", "Device-code login failed"))
    return result

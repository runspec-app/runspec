"""
config.py — resolve the fleet's configuration from one place.

Three sources, in order of the operator's choosing (``load_config``):

1. A single ``runspec_mcp.toml`` (``--config`` / ``$RUNSPEC_MCP_CONFIG`` /
   ``./runspec_mcp.toml`` / ``<app_dir>/runspec_mcp.toml``) with ``[local]``,
   ``[[host]]``, ``[[credential]]`` and ``[ssh]`` sections.
2. An existing runspec-console config **directory** (``--config-dir`` /
   ``$RUNSPEC_MCP_CONFIG_DIR``): read the
   console's own ``runspec_hosts.toml`` + ``runspec_credentials.toml`` +
   ``config.toml`` (``[ssh]`` / ``[local]``) so a console user reuses their fleet
   with zero extra setup.
3. A **config seed** bundled in an installed package (``configseed``): folded in
   as the lowest layer under either of the above, so an air-gapped install can be
   pre-wired. Secrets are never seeded — only metadata.

Everything downstream (catalog, executor, credentials) reads the resolved
``FleetConfig``. Pure-ish: file reads only, no network.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from . import configmerge, configseed
from .hosts import load_hosts, resolve_local_venvs

APP_NAME = "runspec-mcp"


def app_dir() -> Path:
    """Machine-local dir for the fleet's own config/state (manifest, seed stamp)."""
    override = os.environ.get("RUNSPEC_MCP_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def default_config_dir() -> Path | None:
    """A console config directory named by ``$RUNSPEC_MCP_CONFIG_DIR``, if any.

    The environment form of ``--config-dir``, for MCP clients that start the
    gateway with environment variables but no arguments (the Claude Code plugin)."""
    env = os.environ.get("RUNSPEC_MCP_CONFIG_DIR")
    return Path(env).expanduser() if env else None


def host_identity(host: dict[str, Any]) -> str | None:
    """A host entry's own SSH key: ``identity_file`` (runspec_mcp.toml) or
    ``identityFile`` (the console's runspec_hosts.toml spelling)."""
    return host.get("identity_file") or host.get("identityFile") or None


def default_config_path() -> Path | None:
    """The config file to load when none is given on the command line."""
    env = os.environ.get("RUNSPEC_MCP_CONFIG")
    if env:
        return Path(env).expanduser()
    for candidate in (Path.cwd() / "runspec_mcp.toml", app_dir() / "runspec_mcp.toml"):
        if candidate.is_file():
            return candidate
    return None


def manifest_path() -> Path:
    """Where the autonomy manifest is written for the PreToolUse hook to read.

    ``$RUNSPEC_PLUGIN_MANIFEST``, else ``autonomy.json`` in the app dir — the
    user's own config dir, always writable. (It used to live in the plugin
    directory, inside the installed package, which a system-wide install can't
    write; the hook then asked about every call.) The hook resolves it the same
    way (``plugin/hooks/autonomy_gate.py``)."""
    override = os.environ.get("RUNSPEC_PLUGIN_MANIFEST")
    if override:
        return Path(override).expanduser()
    return app_dir() / "autonomy.json"


@dataclass
class FleetConfig:
    """The resolved fleet configuration."""

    hosts: list[dict[str, Any]] = field(default_factory=list)
    credentials: list[dict[str, Any]] = field(default_factory=list)
    ssh: dict[str, Any] = field(default_factory=dict)
    local_venv_globs: list[str] = field(default_factory=list)
    refresh_interval: int = 30
    server_name: str = "runspec-mcp"
    source: str = ""

    def has_local(self) -> bool:
        return any(not str(h.get("ssh") or "").strip() for h in self.hosts) or bool(self.local_venv_globs)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _seed_layers(name: str, base: dict[str, Any]) -> dict[str, Any]:
    """Fold any bundled config-seed layers for ``name`` *under* ``base`` (base wins).

    Returns the merged native-shaped table. No-op when nothing is seeded."""
    fetched, _stamp = configseed.discover()
    layers = fetched.get(name)
    if not layers:
        return base
    parsed_layers = []
    for raw in layers:
        try:
            parsed_layers.append(tomllib.loads(raw.decode("utf-8")))
        except Exception:
            continue
    # Local (base) is the highest-precedence layer; seeds are lower.
    parsed_layers.append(base)
    return configmerge.fold_layers(name, parsed_layers)


def _collection(name: str, key: str, base: list[dict[str, Any]]) -> list[dict[str, Any]]:
    top, _id = configmerge.COLLECTION_FILES[name]
    folded = _seed_layers(name, {top: base})
    return folded.get(top, base)


def load_config(
    config_path: Path | None = None,
    config_dir: Path | None = None,
) -> FleetConfig:
    """Resolve the fleet configuration.

    ``config_dir`` (a runspec-console config directory) takes precedence when
    given; otherwise ``config_path`` is read as a single ``runspec_mcp.toml``.
    With neither, ``$RUNSPEC_MCP_CONFIG_DIR`` names a console directory, else the
    default ``runspec_mcp.toml`` is found. Config seeds are folded under whichever
    wins."""
    fleet_section: dict[str, Any] = {}
    if config_dir is None and config_path is None:
        config_dir = default_config_dir()
    if config_dir is not None:
        d = Path(config_dir).expanduser()
        hosts = load_hosts(d / "runspec_hosts.toml")
        creds_raw = _read_toml(d / "runspec_credentials.toml")
        credentials = creds_raw.get("credential", [])
        cfg = _read_toml(d / "config.toml")
        ssh = cfg.get("ssh", {}) or {}
        local = (cfg.get("local", {}) or {}).get("venv_globs", []) or []
        fleet_section = cfg.get("gateway", {}) or {}
        source = str(d)
    else:
        path = config_path or default_config_path()
        data = _read_toml(path) if path else {}
        hosts = data.get("host", [])
        credentials = data.get("credential", [])
        ssh = data.get("ssh", {}) or {}
        local = (data.get("local", {}) or {}).get("venv_globs", []) or []
        fleet_section = data.get("gateway", {}) or {}
        source = str(path) if path else "(none)"

    # Fold config-seed layers underneath the resolved values (local wins).
    hosts = _collection("runspec_hosts.toml", "name", hosts)
    credentials = _collection("runspec_credentials.toml", "id", credentials)
    # A credential's env var prefix comes from its label; a hand-written entry
    # often gives only `id` (as the docs' own example does), which used to yield
    # no env vars at all — so the secret silently never reached the runnable.
    credentials = [{**c, "label": c.get("label") or c.get("id", "")} for c in credentials]
    # A host's own key: the console writes `identityFile`, runspec_mcp.toml uses
    # `identity_file`. Normalise to the one name every reader uses, so a per-host
    # key from a console directory isn't silently ignored.
    hosts = [{**h, "identity_file": host_identity(h)} if host_identity(h) else h for h in hosts]

    refresh_interval = int(fleet_section.get("refresh_interval", 30) or 30)
    server_name = str(fleet_section.get("server_name", "") or "runspec-mcp")

    return FleetConfig(
        hosts=hosts,
        credentials=credentials,
        ssh=ssh,
        local_venv_globs=[str(g) for g in local],
        refresh_interval=refresh_interval,
        server_name=server_name,
        source=source,
    )


def local_runspec_paths(cfg: FleetConfig) -> list[str]:
    """The local machine's runspec binaries (this venv first, then the globs)."""
    own = ""
    # The venv this gateway runs in — its own runspec, if present.
    scripts = Path(sys.executable).parent
    for name in ("runspec.exe", "runspec"):
        cand = scripts / name
        if cand.exists():
            own = str(cand)
            break
    return resolve_local_venvs(cfg.local_venv_globs, own_path=own)

"""
config.py — resolve the fleet's configuration from one place.

Three sources, in order of the operator's choosing (``load_config``):

1. A single ``runspec_mcp.toml`` (``--config`` / ``$RUNSPEC_MCP_CONFIG`` /
   ``./runspec_mcp.toml`` / ``<app_dir>/runspec_mcp.toml``) with ``[local]``,
   ``[[host]]``, ``[[credential]]`` and ``[ssh]`` sections.
2. An existing runspec-console config **directory** (``--config-dir``): read the
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

    The hook resolves it the same way: ``$RUNSPEC_PLUGIN_MANIFEST`` →
    ``$CLAUDE_PLUGIN_ROOT/autonomy.json`` → the plugin root. We mirror that so the
    gateway writes exactly where the hook looks."""
    override = os.environ.get("RUNSPEC_PLUGIN_MANIFEST")
    if override:
        return Path(override).expanduser()
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        return Path(plugin_root) / "autonomy.json"
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
    given; otherwise ``config_path`` (or the discovered default) is read as a
    single ``runspec_mcp.toml``. Config seeds are folded under whichever wins."""
    fleet_section: dict[str, Any] = {}
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

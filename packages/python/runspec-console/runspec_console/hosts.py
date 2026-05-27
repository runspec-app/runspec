"""
hosts.py — read and write runspec_hosts.toml.

A host entry:

  [[host]]
  name         = "prod-1"           # display name, used as the host key everywhere
  ssh          = "deploy@prod-1"    # SSH connection string; absent = local Windows host
  runspec_path = "/opt/venvs/platform-core/bin/runspec"
  group        = "Production"       # sidebar label (optional)
  role         = "primary"          # "primary" | "secondary" (optional)

  [[host]]
  name         = "local-ops"
  runspec_path = "C:\\venvs\\ops-tools\\Scripts\\runspec.exe"
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def load_hosts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return data.get("host", [])


def save_hosts(path: Path, hosts: list[dict[str, Any]]) -> None:
    lines = []
    for h in hosts:
        lines.append("[[host]]")
        for k, v in h.items():
            lines.append(f'{k} = {_toml_value(v)}')
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def venv_root(runspec_path: str) -> Path:
    """Derive the venv root from the runspec binary path."""
    return Path(runspec_path).parent.parent


def venv_name(runspec_path: str) -> str:
    return venv_root(runspec_path).name


def scripts_dir(runspec_path: str) -> Path:
    return Path(runspec_path).parent


def site_packages(runspec_path: str) -> Path | None:
    root = venv_root(runspec_path)
    # Windows layout
    win = root / "Lib" / "site-packages"
    if win.exists():
        return win
    # Linux/Mac layout
    lib = root / "lib"
    if lib.exists():
        for d in lib.iterdir():
            if d.name.startswith("python") and (d / "site-packages").exists():
                return d / "site-packages"
    return None


def _toml_value(v: Any) -> str:
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)

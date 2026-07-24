"""
hosts.py — read and write runspec_hosts.toml.

A host entry:

  [[host]]
  name          = "prod-1"          # display name, used as the host key everywhere
  ssh           = "deploy@prod-1"   # SSH connection string; absent = local Windows host
  runspec_paths = ["/opt/venvs/platform-core/bin/runspec", "/opt/venvs/ops/bin/runspec"]
  group         = "Production"      # sidebar label (optional)
  role          = "primary"         # "primary" | "secondary" (optional)

  [[host]]
  name          = "old-centos6-1"
  ssh           = "svc@old-centos6-1"
  jump          = "bastion"         # reach this host via system-ssh ProxyJump
  runspec_paths = ["/opt/venvs/ops/bin/runspec"]

  # `jump` selects the system-ssh transport (OpenSSH, not paramiko), so
  # ~/.ssh/config legacy algorithms apply — the way to reach CentOS 6 hosts
  # offering only ssh-rsa/ssh-dss, which paramiko 4+/5 can't negotiate. Put the
  # legacy `HostKeyAlgorithms +ssh-rsa` etc. on the target's Host block in
  # ~/.ssh/config. Requires an OpenSSH client on the console machine.

  [[host]]
  name          = "local-ops"
  runspec_paths = ["C:\\venvs\\ops-tools\\Scripts\\runspec.exe"]

Legacy single-path form is still accepted for backward compatibility:
  runspec_path = "/path/to/runspec"
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
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def validate_editor_hosts(hosts: list[dict[str, Any]]) -> list[str]:
    """Problems with an editor-shaped host list (``get_jump_hosts`` shape), or
    ``[]`` when it is saveable. ``save_jump_hosts`` coerces rather than rejects,
    so this is the pre-flight for callers (the agent's ``manage_hosts``) that
    must refuse bad input with a message instead of silently normalising it.
    """
    errors: list[str] = []
    seen: set[str] = set()
    for i, h in enumerate(hosts):
        label = str(h.get("name") or "").strip() or f"entry {i + 1}"
        name = str(h.get("name") or "").strip()
        if not name:
            errors.append(f"{label}: host 'name' is required and cannot be blank")
        elif name.casefold() in seen:
            errors.append(f"{label}: duplicate host name")
        else:
            seen.add(name.casefold())
        paths = h.get("runspec_paths")
        if paths is not None and (
            not isinstance(paths, list)
            or any(not isinstance(p, str) or not p.strip() for p in paths)
        ):
            errors.append(
                f"{label}: 'runspec_paths' must be a list of non-empty strings"
            )
        port = h.get("port")
        if port not in (None, ""):
            try:
                port_n = int(port)
            except (TypeError, ValueError):
                port_n = 0
            if not 1 <= port_n <= 65535:
                errors.append(f"{label}: 'port' must be an integer between 1 and 65535")
        profiles = h.get("profiles")
        if profiles is not None and (
            not isinstance(profiles, list)
            or any(not isinstance(p, str) for p in profiles)
        ):
            errors.append(f"{label}: 'profiles' must be a list of strings")
        jump = h.get("jump")
        if jump is not None and jump != "":
            if not isinstance(jump, str) or not jump.strip():
                errors.append(f"{label}: 'jump' must be a non-empty SSH host string")
            elif (
                not str(h.get("ssh") or "").strip()
                and not str(h.get("hostname") or "").strip()
            ):
                errors.append(
                    f"{label}: 'jump' needs an ssh/hostname target to reach through it"
                )
    return errors


def group_members(hosts: list[dict[str, Any]], group: str) -> list[str]:
    """Host names whose ``group`` equals ``group`` (exact, case-insensitive), in
    config order. The single source of truth for "which hosts are in this group",
    reused by the agent, triggers, schedules, and the fan-out executor.

    An empty/blank ``group`` returns ``[]`` (the ungrouped "Hosts" section is not
    a targetable group). Hosts whose own ``group`` is empty never match.
    """
    want = str(group or "").strip().casefold()
    if not want:
        return []
    out: list[str] = []
    for h in hosts:
        hg = str(h.get("group") or "").strip()
        if hg and hg.casefold() == want and h.get("name"):
            out.append(str(h["name"]))
    return out


def groups_map(hosts: list[dict[str, Any]]) -> dict[str, list[str]]:
    """``{group name: [host names]}`` over all hosts that declare a non-empty
    ``group``, preserving first-seen group order and member config order."""
    out: dict[str, list[str]] = {}
    for h in hosts:
        hg = str(h.get("group") or "").strip()
        if not hg or not h.get("name"):
            continue
        out.setdefault(hg, []).append(str(h["name"]))
    return out


def host_profiles(host: dict[str, Any]) -> list[str]:
    """A host's profile tags (its ``profiles`` array), as a clean list of strings.

    Accepts the array form (``profiles = ["app-support", "devops"]``); blank/
    non-string members are dropped. A host with no ``profiles`` belongs to no
    custom profile (visible only under the built-in *All hosts*)."""
    raw = host.get("profiles")
    if not isinstance(raw, list):
        return []
    return [str(p).strip() for p in raw if str(p).strip()]


def host_in_profile(host: dict[str, Any], profile: str) -> bool:
    """True if ``host`` carries ``profile`` (exact, case-insensitive). A blank
    ``profile`` means *All hosts* — every host is in scope."""
    want = str(profile or "").strip().casefold()
    if not want:
        return True
    return any(p.casefold() == want for p in host_profiles(host))


def profile_members(hosts: list[dict[str, Any]], profile: str) -> list[str]:
    """Host names tagged with ``profile`` (exact, case-insensitive), in config
    order. A blank ``profile`` returns every named host (the *All hosts* scope)."""
    out: list[str] = []
    for h in hosts:
        if h.get("name") and host_in_profile(h, profile):
            out.append(str(h["name"]))
    return out


def profiles_map(hosts: list[dict[str, Any]]) -> dict[str, list[str]]:
    """``{profile name: [host names]}`` over all hosts that declare a non-empty
    ``profiles`` tag, preserving first-seen profile order and member config order."""
    out: dict[str, list[str]] = {}
    for h in hosts:
        if not h.get("name"):
            continue
        for p in host_profiles(h):
            out.setdefault(p, []).append(str(h["name"]))
    return out


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
    if isinstance(v, list):
        return (
            "[" + ", ".join(f'"{x}"' if isinstance(x, str) else str(x) for x in v) + "]"
        )
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)

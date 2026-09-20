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

The synthetic ``local`` host (the console's own machine) is not configured here.
Its own venv is always discovered automatically; additional local venvs are
resolved from the ``[local] venv_globs`` convention in config.toml (see
:func:`resolve_local_venvs` and ``config.read_local``), so one synced/seedable
glob (e.g. ``C:\\venvs\\*``) works across a fleet of identical machines.
"""

from __future__ import annotations

import glob as _glob
import logging
import os
from pathlib import Path
from typing import Any

from .tomlsafe import basic_string, dump_scalar, load_table_array, quarantine_corrupt

logger = logging.getLogger(__name__)


def _quarantine_corrupt(path: Path, err: Exception) -> None:
    """Move an unreadable hosts file aside so it stops crashing every read
    (thin wrapper over the shared :func:`tomlsafe.quarantine_corrupt`)."""
    quarantine_corrupt(path, err, log=logger)


def load_hosts(path: Path) -> list[dict[str, Any]]:
    # A malformed hosts file must never crash the app or block launch — this runs
    # on the startup path (Bridge.__init__ -> _reload_hosts). Quarantine it and
    # start with no configured hosts rather than raising.
    return load_table_array(path, "host", log=logger)


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


# Probe order for a runspec binary inside a venv root — Windows Scripts\ first,
# then POSIX bin/ (mirrors _local_host_entry and run_local's candidate order).
_RUNSPEC_IN_VENV = ("Scripts/runspec.exe", "bin/runspec")


def resolve_local_venvs(globs: list[str], *, own_path: str) -> list[str]:
    """Resolve the local machine's runspec binaries from a set of venv-root globs.

    This is the local mirror of a remote host's ``runspec_paths`` list: one binary
    per venv, from which :func:`venv_name` derives the group everything downstream
    keys on (per-venv discovery, group-matched execution, per-venv sidebar counts).

    ``own_path`` is the runspec binary of the venv the console itself runs in — it
    is always **first** in the result (the default venv) and always present, so
    local behavior is unchanged when no globs are configured. Each glob is expanded
    non-recursively (:func:`glob.glob`); every matched **directory** is treated as
    a candidate venv root and probed for a runspec binary in the usual layout
    (``Scripts/runspec.exe`` on Windows, then ``bin/runspec`` on POSIX). Roots
    without one are skipped, so a glob like ``C:\\venvs\\*`` that also matches
    non-venv dirs is self-filtering. Results are de-duplicated by real path, so a
    glob that also matches the console's own venv doesn't list it twice.
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(p: str) -> None:
        try:
            key = os.path.realpath(p)
        except OSError:
            key = p
        if key in seen:
            return
        seen.add(key)
        out.append(p)

    if own_path:
        _add(own_path)
    for pattern in globs or []:
        pat = str(pattern or "").strip()
        if not pat:
            continue
        for match in sorted(_glob.glob(pat)):
            root = Path(match)
            if not root.is_dir():
                continue
            for rel in _RUNSPEC_IN_VENV:
                candidate = root / rel
                if candidate.exists():
                    _add(str(candidate))
                    break
    return out


def _toml_string(s: str) -> str:
    """A quoted TOML basic string (shared audited serializer)."""
    return basic_string(s)


def _toml_value(v: Any) -> str:
    """A TOML scalar/list value; ``None`` (or any non-TOML value) becomes ``""``
    rather than a Python ``repr`` that would corrupt the file and wedge every
    later ``load_hosts`` / ``save_jump_hosts`` (shared audited serializer)."""
    return dump_scalar(v)

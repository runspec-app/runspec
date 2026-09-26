"""Package-manager helpers: detect the distro's manager, then list/upgrade/install/remove/autoremove.

Supports the common Linux families: Debian/Ubuntu (``apt-get`` + ``dpkg``),
RHEL/Fedora (``dnf``/``yum`` + ``rpm``), SUSE (``zypper`` + ``rpm``), and Arch
(``pacman``). The read operations (list installed, list upgrades) return plain
data; the write operations (upgrade, install, remove, autoremove) need root and raise
``CommandError`` when the underlying command exits non-zero (for example when
run as an unprivileged user). ``ToolNotFoundError`` is raised when no supported
package manager is on PATH.
"""

import os
import re
import shutil
import subprocess

from runspec_linux_core.errors import CommandError, ToolNotFoundError

# Ordered by preference; the first manager found on PATH wins.
_MANAGERS = ("apt-get", "dnf", "yum", "zypper", "pacman")

# The argv prefix for each manager's write operations. ``-y`` /
# ``--non-interactive`` / ``--noconfirm`` keep them unattended (these run over
# SSH with no TTY). Package names are appended after validation.
_UPGRADE_CMDS = {
    "apt-get": ["apt-get", "-y", "upgrade"],
    "dnf": ["dnf", "-y", "upgrade"],
    "yum": ["yum", "-y", "update"],
    "zypper": ["zypper", "--non-interactive", "update"],
    "pacman": ["pacman", "-Syu", "--noconfirm"],
}
_INSTALL_CMDS = {
    "apt-get": ["apt-get", "install", "-y"],
    "dnf": ["dnf", "install", "-y"],
    "yum": ["yum", "install", "-y"],
    "zypper": ["zypper", "--non-interactive", "install"],
    "pacman": ["pacman", "-S", "--noconfirm"],
}
_REMOVE_CMDS = {
    "apt-get": ["apt-get", "remove", "-y"],
    "dnf": ["dnf", "remove", "-y"],
    "yum": ["yum", "remove", "-y"],
    "zypper": ["zypper", "--non-interactive", "remove"],
    "pacman": ["pacman", "-R", "--noconfirm"],
}
# Purge also deletes the packages' configuration files. Only apt and pacman
# have a separate mode for it; rpm-based managers already drop unmodified
# config files on remove (and keep edited ones as ``.rpmsave`` either way).
_PURGE_CMDS = {
    "apt-get": ["apt-get", "purge", "-y"],
    "pacman": ["pacman", "-Rn", "--noconfirm"],
}

# A conservative package-token shape: starts alphanumeric, then the punctuation
# real package names use. Rejects whitespace, shell metacharacters, and leading
# dashes (which a manager would parse as an option) before anything reaches argv.
_PKG_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:@-]*$")


def detect_manager() -> str:
    """Return the name of the first supported package manager found on PATH.

    Raises ToolNotFoundError if none of apt-get/dnf/yum/zypper/pacman are present.
    """
    for mgr in _MANAGERS:
        if shutil.which(mgr):
            return mgr
    raise ToolNotFoundError("No supported package manager found (looked for: " + ", ".join(_MANAGERS) + ")")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    # DEBIAN_FRONTEND keeps apt from trying to prompt over a non-interactive
    # SSH channel; it is harmless for the other managers.
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def _tail(output: str, limit: int = 40) -> list[str]:
    """Return the last ``limit`` non-empty-trimmed lines so payloads stay bounded."""
    lines = output.strip().splitlines()
    return lines[-limit:]


def _validate_packages(packages: str) -> list[str]:
    """Split a space-separated package string into validated tokens.

    Raises ValueError if the string is empty or any token is not a plausible
    package name (this blocks option injection and shell metacharacters before
    the names reach argv).
    """
    names = packages.split()
    if not names:
        raise ValueError("no package name given")
    for name in names:
        if not _PKG_TOKEN.match(name):
            raise ValueError(f"invalid package name: {name!r}")
    return names


def list_packages(name_filter: str | None = None, limit: int = 100, reason: bool = False) -> dict:
    """Return installed packages (name + version), optionally filtered by substring.

    Read-only. Results are sorted by name and capped at ``limit``; ``total`` is
    the count before the cap. With ``reason`` each row also carries
    ``installed``: ``"manual"`` (asked for by name) or ``"auto"`` (pulled in as a
    dependency) — the flag autoremove goes by, so a manual package is never
    autoremoved however old. It comes from ``apt-mark showmanual``, ``pacman
    -Qqe``, ``dnf repoquery --userinstalled`` or ``zypper packages
    --userinstalled``; it is ``null`` on yum or when that query fails. Raises
    ToolNotFoundError if no package manager is present, or CommandError if the
    query command fails.
    """
    mgr = detect_manager()
    if mgr == "apt-get":
        result = _run(["dpkg-query", "-W", "-f=${Package}\t${Version}\n"])
        rows = _parse_two_col(result.stdout)
    elif mgr == "pacman":
        result = _run(["pacman", "-Q"])
        rows = _parse_pacman_q(result.stdout)
    else:  # dnf / yum / zypper are all rpm-based
        result = _run(["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\n"])
        rows = _parse_two_col(result.stdout)

    if not rows and result.returncode != 0:
        raise CommandError(result.stderr.strip() or "package query failed")

    if name_filter:
        needle = name_filter.lower()
        rows = [r for r in rows if needle in r["name"].lower()]
    rows.sort(key=lambda r: r["name"])
    total = len(rows)
    rows = rows[:limit]
    if reason:
        manual = _manual_packages(mgr)
        for row in rows:
            row["installed"] = None if manual is None else ("manual" if row["name"] in manual else "auto")
    return {"manager": mgr, "total": total, "count": min(total, limit), "packages": rows}


def _manual_packages(mgr: str) -> set[str] | None:
    """Names of the packages installed by request rather than as a dependency.

    ``None`` when the manager can't say (yum) or the query fails, so a caller
    reports "unknown" instead of guessing.
    """
    if mgr == "apt-get":
        cmd = ["apt-mark", "showmanual"]
    elif mgr == "pacman":
        cmd = ["pacman", "-Qqe"]
    elif mgr == "dnf":
        cmd = ["dnf", "-q", "repoquery", "--userinstalled", "--qf", "%{name}\n"]
    elif mgr == "zypper":
        run = _run(["zypper", "--non-interactive", "packages", "--userinstalled"])
        if run.returncode != 0:
            return None
        return {row["name"] for row in _parse_zypper_packages(run.stdout)}
    else:
        return None
    run = _run(cmd)
    if run.returncode != 0:
        return None
    return {line.strip() for line in run.stdout.splitlines() if line.strip()}


def list_upgrades(refresh: bool = True) -> dict:
    """Return packages with an available upgrade (name, current, candidate).

    When ``refresh`` is true the package index is updated first (this needs root
    on most systems); pass ``refresh=False`` to read the cached index as any
    user. Raises ToolNotFoundError if no package manager is present, or
    CommandError if a refresh or query command fails.
    """
    mgr = detect_manager()
    if mgr == "apt-get":
        if refresh:
            _check(_run(["apt-get", "update", "-q"]), "apt-get update")
        # `apt list --upgradable` reads the cache without root (it warns about an
        # unstable CLI on stderr, which we ignore).
        result = _run(["apt", "list", "--upgradable"])
        upgrades = _parse_apt_list(result.stdout)
    elif mgr in ("dnf", "yum"):
        cmd = [mgr, "check-update"]
        if not refresh:
            cmd.append("-C")  # cache-only
        result = _run(cmd)
        # check-update exits 100 when updates exist, 0 when none — neither is an error.
        if result.returncode not in (0, 100):
            raise CommandError(result.stderr.strip() or f"{mgr} check-update failed")
        upgrades = _parse_dnf_check(result.stdout)
    elif mgr == "zypper":
        if refresh:
            _check(_run(["zypper", "--non-interactive", "refresh"]), "zypper refresh")
        result = _run(["zypper", "--non-interactive", "list-updates"])
        upgrades = _parse_zypper_updates(result.stdout)
    else:  # pacman
        if refresh:
            _check(_run(["pacman", "-Sy"]), "pacman -Sy")
        # `pacman -Qu` exits 1 when there are simply no updates; that is not an error.
        result = _run(["pacman", "-Qu"])
        upgrades = _parse_pacman_qu(result.stdout)

    return {"manager": mgr, "refreshed": refresh, "count": len(upgrades), "upgrades": upgrades}


def upgrade_packages() -> dict:
    """Refresh the package index and upgrade all installed packages.

    Needs root. Returns ``{manager, command, upgraded: True, output}`` where
    ``output`` is a bounded tail of the command's stdout. Raises
    ToolNotFoundError if no package manager is present, or CommandError if the
    refresh or upgrade command fails.
    """
    mgr = detect_manager()
    if mgr == "apt-get":
        _check(_run(["apt-get", "update", "-q"]), "apt-get update")
    cmd = _UPGRADE_CMDS[mgr]
    result = _run(cmd)
    if result.returncode != 0:
        raise CommandError(result.stderr.strip() or " ".join(cmd) + " failed")
    return {"manager": mgr, "command": " ".join(cmd), "upgraded": True, "output": _tail(result.stdout)}


def install_package(packages: str) -> dict:
    """Install one or more packages (space-separated). Needs root.

    Returns ``{manager, packages, installed: True, output}``. Raises ValueError
    on an empty/invalid name, ToolNotFoundError if no package manager is present,
    or CommandError if the install command fails.
    """
    names = _validate_packages(packages)
    mgr = detect_manager()
    cmd = [*_INSTALL_CMDS[mgr], *names]
    result = _run(cmd)
    if result.returncode != 0:
        raise CommandError(result.stderr.strip() or " ".join(cmd) + " failed")
    return {"manager": mgr, "packages": names, "installed": True, "output": _tail(result.stdout)}


def remove_package(packages: str, purge: bool = False) -> dict:
    """Remove one or more packages (space-separated). Needs root.

    ``purge`` also deletes their configuration files (``apt-get purge``,
    ``pacman -Rn``). dnf/yum/zypper have no separate purge mode, so there it is
    a plain remove and ``purged`` comes back False.

    Returns ``{manager, packages, removed: True, purged, output}``. Raises
    ValueError on an empty/invalid name, ToolNotFoundError if no package manager
    is present, or CommandError if the remove command fails.
    """
    names = _validate_packages(packages)
    mgr = detect_manager()
    purged = purge and mgr in _PURGE_CMDS
    cmd = [*(_PURGE_CMDS[mgr] if purged else _REMOVE_CMDS[mgr]), *names]
    result = _run(cmd)
    if result.returncode != 0:
        raise CommandError(result.stderr.strip() or " ".join(cmd) + " failed")
    return {"manager": mgr, "packages": names, "removed": True, "purged": purged, "output": _tail(result.stdout)}


def autoremove_packages(purge: bool = False, dry_run: bool = False) -> dict:
    """Remove packages nothing depends on any more (apt-get autoremove and friends). Needs root.

    The candidates are listed first and returned as ``packages``, so the caller
    sees exactly what went (or, with ``dry_run``, what would go). Nothing runs
    when there are no candidates. On Debian/Ubuntu apt keeps the running kernel
    and the newest ones, so old kernels are removed and the booted one never is.

    Per manager: apt-get ``autoremove`` (``--purge`` with ``purge``); dnf/yum
    ``autoremove``; zypper removes its ``--unneeded`` packages; pacman removes
    orphans (``-Qdt``, with ``-n`` for ``purge``). ``purge`` only changes apt
    and pacman (``purged`` says whether it applied). yum can't list candidates
    without removing them, so ``dry_run`` is refused there and a real run
    returns ``packages: null``.

    Returns ``{manager, dry_run, purged, count, packages, removed, output}``.
    Raises ToolNotFoundError if no package manager is present, or CommandError
    if listing or removing fails.
    """
    mgr = detect_manager()
    purged = purge and mgr in _PURGE_CMDS
    if mgr == "yum" and dry_run:
        raise CommandError("yum can't list autoremove candidates without removing them; dry run unsupported")

    candidates = None if mgr == "yum" else _autoremove_candidates(mgr, purged)
    result = {
        "manager": mgr,
        "dry_run": dry_run,
        "purged": purged,
        "count": None if candidates is None else len(candidates),
        "packages": candidates,
        "removed": False,
        "output": [],
    }
    if dry_run or candidates == []:
        return result

    if mgr == "apt-get":
        cmd = ["apt-get", "autoremove", "-y", *(["--purge"] if purged else [])]
    elif mgr in ("dnf", "yum"):
        cmd = [mgr, "autoremove", "-y"]
    elif mgr == "zypper":
        cmd = ["zypper", "--non-interactive", "remove", *[c["name"] for c in candidates or []]]
    else:  # pacman
        cmd = ["pacman", "-Rns" if purged else "-Rs", "--noconfirm", *[c["name"] for c in candidates or []]]
    run = _run(cmd)
    if run.returncode != 0:
        raise CommandError(run.stderr.strip() or " ".join(cmd) + " failed")
    result["removed"] = True
    result["output"] = _tail(run.stdout)
    return result


def list_autoremovable(purge: bool = False) -> dict:
    """List what ``autoremove_packages`` would remove, removing nothing.

    Read-only and needs no root (apt's simulation, dnf repoquery, zypper's
    package list and pacman's orphan query all run unprivileged). ``purge``
    matters only on apt, where the simulation then shows purges. Returns
    ``{manager, purged, count, packages}``. Raises CommandError on yum, which
    can't list candidates without removing them, or if the listing fails.
    """
    mgr = detect_manager()
    if mgr == "yum":
        raise CommandError("yum can't list autoremove candidates without removing them")
    purged = purge and mgr in _PURGE_CMDS
    candidates = _autoremove_candidates(mgr, purged)
    return {"manager": mgr, "purged": purged, "count": len(candidates), "packages": candidates}


def _autoremove_candidates(mgr: str, purge: bool) -> list[dict]:
    """List what an autoremove would take, as ``{name, version}`` rows."""
    if mgr == "apt-get":
        cmd = ["apt-get", "-s", "autoremove", *(["--purge"] if purge else [])]
        run = _run(cmd)
        _check(run, " ".join(cmd))
        return _parse_apt_simulate(run.stdout)
    if mgr == "dnf":
        cmd = ["dnf", "-q", "repoquery", "--unneeded", "--qf", "%{name}\t%{version}-%{release}\n"]
        run = _run(cmd)
        _check(run, " ".join(cmd))
        return _parse_two_col(run.stdout)
    if mgr == "zypper":
        cmd = ["zypper", "--non-interactive", "packages", "--unneeded"]
        run = _run(cmd)
        _check(run, " ".join(cmd))
        return _parse_zypper_packages(run.stdout)
    # pacman: `-Qdt` exits 1 with no output when there are no orphans.
    run = _run(["pacman", "-Qdt"])
    if run.returncode != 0 and run.stdout.strip():
        raise CommandError(run.stderr.strip() or "pacman -Qdt failed")
    return _parse_pacman_q(run.stdout)


def _check(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        raise CommandError(result.stderr.strip() or f"{label} failed")


def _parse_two_col(output: str) -> list[dict]:
    """Parse tab-separated ``name<TAB>version`` lines (dpkg-query / rpm output)."""
    rows = []
    for line in output.strip().splitlines():
        if "\t" not in line:
            continue
        name, version = line.split("\t", 1)
        name = name.strip()
        if name:
            rows.append({"name": name, "version": version.strip()})
    return rows


def _parse_pacman_q(output: str) -> list[dict]:
    rows = []
    for line in output.strip().splitlines():
        parts = line.split(None, 1)
        if parts:
            rows.append({"name": parts[0], "version": parts[1] if len(parts) > 1 else ""})
    return rows


def _parse_apt_list(output: str) -> list[dict]:
    """Parse ``apt list --upgradable`` lines: ``name/repo candidate arch [upgradable from: current]``."""
    rows = []
    for line in output.splitlines():
        if "/" not in line or "[upgradable from:" not in line:
            continue
        name = line.split("/", 1)[0].strip()
        parts = line.split()
        candidate = parts[1] if len(parts) > 1 else ""
        m = re.search(r"\[upgradable from:\s*([^\]]+)\]", line)
        current = m.group(1).strip() if m else ""
        rows.append({"name": name, "current": current, "candidate": candidate})
    return rows


def _parse_dnf_check(output: str) -> list[dict]:
    """Parse ``dnf/yum check-update`` rows: ``name.arch  candidate  repo`` (no current version)."""
    rows = []
    for raw in output.splitlines():
        line = raw.rstrip()
        if not line or line.startswith(("Last metadata", "Obsoleting", "Security:")):
            continue
        parts = line.split()
        if len(parts) != 3 or "." not in parts[0]:
            continue
        name = parts[0].rsplit(".", 1)[0]
        rows.append({"name": name, "current": "", "candidate": parts[1]})
    return rows


def _parse_zypper_updates(output: str) -> list[dict]:
    """Parse the ``v |`` rows of ``zypper list-updates`` (pipe-separated columns)."""
    rows = []
    for line in output.splitlines():
        if "|" not in line:
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 6 or cols[0] != "v":
            continue
        rows.append({"name": cols[2], "current": cols[3], "candidate": cols[4]})
    return rows


def _parse_apt_simulate(output: str) -> list[dict]:
    """Parse ``apt-get -s`` removal lines: ``Remv name [version]`` / ``Purg name [version]``."""
    rows = []
    for line in output.splitlines():
        m = re.match(r"(?:Remv|Purg)\s+(\S+)(?:\s+\[([^\]]*)\])?", line)
        if m:
            rows.append({"name": m.group(1), "version": (m.group(2) or "").strip()})
    return rows


def _parse_zypper_packages(output: str) -> list[dict]:
    """Parse ``zypper packages`` table rows: ``S | Repository | Name | Version | Arch``."""
    rows = []
    for line in output.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 5 or cols[0] not in ("i", "i+"):
            continue
        rows.append({"name": cols[2], "version": cols[3]})
    return rows


def _parse_pacman_qu(output: str) -> list[dict]:
    """Parse ``pacman -Qu`` lines: ``name current -> candidate``."""
    rows = []
    for line in output.strip().splitlines():
        m = re.match(r"(\S+)\s+(\S+)\s+->\s+(\S+)", line)
        if m:
            rows.append({"name": m.group(1), "current": m.group(2), "candidate": m.group(3)})
    return rows

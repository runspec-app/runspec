"""
provision.py — build and remove runspec venvs on fleet hosts.

Pure logic behind Settings → Jump Hosts → *Provision venv…* / *Deprovision…*:
validation, the scripts each step runs on a host, and the parsers for their
output. Steps that run as another account (the run-as account, or root for the
folder step) are small stdlib Python scripts run on the chosen interpreter
(``<python> -I -S -c <script>``), never ``sh -c``: a sudoers rule that lets the
SSH login act as a service account but refuses shells still covers them, the
same reason runspec 0.70.0 moved the run env prelude onto the venv's python.
Steps that run as the SSH login itself (probe, root check, inspect) stay POSIX
``sh``. The bridge (:mod:`bridge_provision`) owns the I/O — it runs
each script over the host's pooled SSH connection through
:func:`marketplace.remote_command`, so a ``run_as`` service account is reached
with ``sudo -n -u`` / ``su`` and the package-index URL (which can carry a
credential) rides the SPEC env frame on stdin, never the command line.

A provision is, per host:

1. **probe** — list the Python interpreters on the host (operator picks one; a
   non-standard location is reachable through the browse popup), as the SSH user;
2. **preflight** — as the run-as account: the interpreter runs, has ``venv`` +
   ``ensurepip``, is >= 3.10 (runspec's floor), the destination is absent / empty
   / holds only a kept ``logs/``, and its nearest existing ancestor is writable
   (or, when it isn't, the SSH login can ``sudo -n``: then **folder** — root makes
   the empty venv folder and chowns it to the run-as account, the usual case for
   a root-owned ``/opt/venvs`` shared by service accounts);
3. **create** — ``python -m venv --symlinks`` + the permission normalisation the
   removed ``create-venv`` runnable applied (``u=rwX,go=rX``; a kept ``logs/`` is left
   alone);
4. **install** — ``pip install -U pip`` then ``pip install runspec <packages>``;
5. **logs inbox** — ``<venv>/bin/runspec logs share --inbox`` as the owner;
6. **verify** — ``<venv>/bin/runspec --version``;
7. **audit** — ``<venv>/bin/runspec audit record venv.provision`` as the owner,
   so the new venv's own ledger (and the host's syslog) starts with how it was
   made. A deprovision records ``venv.deprovision confirmed`` the same way just
   before the delete (``logger`` to syslog when the venv's runspec is older
   than 0.71.0). The console keeps its own record too.

This module is the one home of the venv recipe. It started as a copy of
``runspec_linux_core.venvs.create_venv`` as remote shell steps; that helper and
the ``create-venv`` / ``install-into-venv`` runnables were removed in
runspec-linux-core 0.14.0 / runspec-linux 0.20.0 (the command line just uses
pip).

A deprovision deletes a venv the console already knows (one of a host's
``runspec_paths``), behind guards that are enforced here *and* re-checked on
the host in the same command as the delete: an absolute, non-system path,
not a symlink, containing ``pyvenv.cfg`` + ``bin/runspec``. ``keep_logs``
removes everything except ``logs/`` so the folder stays with its history (and
a later provision at the same path builds around it).

POSIX hosts only for now; a Windows path (``\\`` / ``.exe``) is refused.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

# runspec's own floor (packages/python/runspec/pyproject.toml requires-python).
MIN_RUNSPEC_PYTHON = (3, 10)

_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*$")
_VENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")

# Directories that must never be a venv root (deprovision would delete them).
# Anything shallower than two components is refused too, and so is a home dir
# itself (``/home/<user>``, ``/Users/<user>``) — see :func:`check_removable`.
_PROTECTED = frozenset(
    {
        "/",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib32",
        "/lib64",
        "/media",
        "/mnt",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/snap",
        "/srv",
        "/sys",
        "/tmp",
        "/usr",
        "/usr/bin",
        "/usr/lib",
        "/usr/lib64",
        "/usr/local",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/share",
        "/var",
        "/var/lib",
        "/var/log",
        "/var/tmp",
        "/opt/venvs",
        "/Users",
        "/Applications",
        "/Library",
        "/System",
    }
)
_HOME_PARENTS = ("/home", "/Users", "/export/home")


class ProvisionError(ValueError):
    """A refused request (bad input / unsafe target) — shown to the operator."""


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate_user(name: str) -> str:
    """A run-as account name, or ``""`` for "the SSH user"."""
    s = (name or "").strip()
    if s and not _USER_RE.match(s):
        raise ProvisionError(f"Not a user name: {name!r}.")
    return s


def validate_become(method: str) -> str:
    m = (method or "sudo").strip().lower()
    if m not in ("sudo", "su"):
        raise ProvisionError("Become method must be sudo or su.")
    return m


def _is_windows_path(path: str) -> bool:
    return (
        "\\" in path
        or path.lower().endswith(".exe")
        or bool(re.match(r"^[A-Za-z]:", path))
    )


def normalize_dest(path: str) -> str:
    """An absolute POSIX venv directory: normalised, no trailing slash, and with a
    venv-ish final component. Raises :class:`ProvisionError` otherwise."""
    p = (path or "").strip()
    if not p:
        raise ProvisionError("Pick a location for the venv.")
    if _is_windows_path(p):
        raise ProvisionError("Only Linux/macOS hosts can be provisioned for now.")
    if not p.startswith("/"):
        raise ProvisionError(f"The location must be an absolute path: {path!r}.")
    if any(c in p for c in "\n\r\0"):
        raise ProvisionError("The location contains a control character.")
    norm = posixpath.normpath(p)
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    name = posixpath.basename(norm)
    if not _VENV_NAME_RE.match(name):
        raise ProvisionError(
            f"Venv folder name {name!r} must start with a letter or digit and use "
            "only letters, digits, '.', '_' or '-'."
        )
    check_not_protected(norm)
    return norm


def check_not_protected(root: str) -> None:
    """Refuse a system directory, a top-level directory, or a home dir itself."""
    if root in _PROTECTED:
        raise ProvisionError(f"{root} is a system directory, not a venv location.")
    parts = [x for x in root.split("/") if x]
    if len(parts) < 2:
        raise ProvisionError(f"{root} is too close to / to be a venv location.")
    parent = posixpath.dirname(root)
    if parent in _HOME_PARENTS:
        raise ProvisionError(f"{root} looks like a home directory, not a venv.")


def venv_paths(dest: str) -> dict[str, str]:
    """The paths inside a (POSIX) venv the steps use."""
    return {
        "root": dest,
        "python": f"{dest}/bin/python",
        "runspec": f"{dest}/bin/runspec",
        "logs": f"{dest}/logs",
    }


def root_from_runspec_path(runspec_path: str) -> str:
    """``/opt/venvs/ops/bin/runspec`` → ``/opt/venvs/ops`` (POSIX only)."""
    p = (runspec_path or "").strip()
    if not p:
        raise ProvisionError("No runspec path given.")
    if _is_windows_path(p):
        raise ProvisionError("Only Linux/macOS venvs can be deprovisioned for now.")
    if not p.startswith("/"):
        raise ProvisionError(
            f"{runspec_path!r} is not an absolute path, so the venv can't be located."
        )
    norm = posixpath.normpath(p)
    if posixpath.basename(norm) == "runspec":
        norm = posixpath.dirname(norm)
    if posixpath.basename(norm) == "bin":
        norm = posixpath.dirname(norm)
    return norm


def parse_version(text: str) -> tuple[int, ...] | None:
    m = _VERSION_RE.match((text or "").strip())
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def version_ok_for_runspec(version: str) -> bool:
    v = parse_version(version)
    return bool(v) and v[:2] >= MIN_RUNSPEC_PYTHON  # type: ignore[index]


def matches_minor(version: str, wanted: str) -> bool:
    """``3.12.4`` matches ``3.12`` (and ``3.12.4``)."""
    v, w = parse_version(version), parse_version(wanted)
    if not v or not w:
        return False
    return v[: len(w)] == w


# --------------------------------------------------------------------------- #
# Interpreter probe
# --------------------------------------------------------------------------- #

PROBE_MARK = "RSPY"

# Run as ``sh -c PROBE_SCRIPT sh [extra paths…]``. Each candidate prints
# ``RSPY<TAB>path<TAB>resolved<TAB>version``. Globs that match nothing stay
# literal and fail the ``-f`` test, so no nullglob is needed.
PROBE_SCRIPT = r"""
found() {
  p=$1
  case "$p" in *-config|*-dbg|*.py) return 0;; esac
  [ -f "$p" ] && [ -x "$p" ] || return 0
  r=$(readlink -f "$p" 2>/dev/null || printf '%s' "$p")
  v=$("$p" -c 'import sys;print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null) || return 0
  printf 'RSPY\t%s\t%s\t%s\n' "$p" "$r" "$v"
}
for p in "$@"; do found "$p"; done
for n in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3.6; do
  p=$(command -v "$n" 2>/dev/null) && found "$p"
done
for p in /usr/bin/python3* /usr/local/bin/python3* /opt/*/bin/python3* \
         /opt/*/*/bin/python3* /opt/rh/*/root/usr/bin/python3* /usr/local/*/bin/python3*; do
  found "$p"
done
printf 'RSID\t%s\t%s\n' "$(id -un 2>/dev/null)" "$(uname -s 2>/dev/null)"
"""


@dataclass
class Interpreter:
    path: str
    real: str
    version: str

    @property
    def runspec_ok(self) -> bool:
        return version_ok_for_runspec(self.version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "real": self.real,
            "version": self.version,
            "runspec_ok": self.runspec_ok,
        }


@dataclass
class ProbeResult:
    user: str = ""
    os: str = ""
    pythons: list[Interpreter] = field(default_factory=list)


def probe_argv(extra_paths: list[str] | None = None) -> list[str]:
    extras = [
        p for p in (extra_paths or []) if isinstance(p, str) and p.startswith("/")
    ]
    return ["sh", "-c", PROBE_SCRIPT, "sh", *extras]


def parse_probe(text: str) -> ProbeResult:
    """Interpreters in discovery order, de-duplicated on the resolved binary
    (the first spelling seen wins, so an explicitly browsed path or the
    ``python3.N`` on PATH is kept over a glob hit)."""
    res = ProbeResult()
    seen: set[str] = set()
    for line in (text or "").splitlines():
        parts = line.rstrip("\r").split("\t")
        if parts[0] == "RSID" and len(parts) >= 3:
            res.user, res.os = parts[1], parts[2]
            continue
        if parts[0] != PROBE_MARK or len(parts) < 4:
            continue
        path, real, version = parts[1], parts[2], parts[3]
        if not parse_version(version):
            continue
        key = real or path
        if key in seen:
            continue
        seen.add(key)
        res.pythons.append(Interpreter(path=path, real=real, version=version))
    return res


def pick_interpreter(pythons: list[Interpreter], wanted: str) -> Interpreter | None:
    """The first interpreter matching ``wanted`` — an absolute path (matched on
    the path or its resolved target) or a version like ``3.12``."""
    w = (wanted or "").strip()
    if not w:
        return None
    if w.startswith("/"):
        for i in pythons:
            if w in (i.path, i.real):
                return i
        return None
    for i in pythons:
        if matches_minor(i.version, w):
            return i
    return None


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #

PREFLIGHT_MARK = "RSPF"

# ``<python> -I -S -c PREFLIGHT_SCRIPT <dest>`` as the run-as account, on the
# interpreter the venv will be built from (so a sudoers rule that refuses
# shells still covers it). Prints ``RSPF<TAB>key<TAB>value`` lines; never
# changes anything. Python 3.6-compatible: an older interpreter must still
# report its version so the verdict can say "too old".
PREFLIGHT_SCRIPT = r"""
import os, sys
os.chdir("/")
dest = sys.argv[1]
def out(k, v):
    sys.stdout.write("RSPF\t%s\t%s\n" % (k, v))
try:
    import pwd
    out("user", pwd.getpwuid(os.geteuid()).pw_name)
except (ImportError, KeyError):
    out("user", "")
out("python_version", "%d.%d.%d" % tuple(sys.version_info[:3]))
try:
    import venv, ensurepip
    out("venv", "ok")
except ImportError:
    out("venv", "missing")
if os.path.islink(dest):
    out("dest", "symlink")
elif os.path.exists(dest) and not os.path.isdir(dest):
    out("dest", "file")
elif os.path.isdir(dest):
    try:
        names = os.listdir(dest)
    except OSError:
        names = []
    if [n for n in names if n != "logs"]:
        out("dest", "notempty")
    elif os.path.isdir(os.path.join(dest, "logs")):
        out("dest", "logs_only")
    else:
        out("dest", "empty")
else:
    out("dest", "absent")
a = dest
while not os.path.exists(a) and a != "/":
    a = os.path.dirname(a)
out("ancestor", a)
out("writable", "yes" if os.access(a, os.W_OK) else "no")
"""


def _py_argv(python: str, script: str, *args: str) -> list[str]:
    """Run ``script`` on ``python``: isolated (``-I``: no PYTHON* env, no user
    site, no cwd on the path) and without ``site`` (``-S``), stdlib only.
    Escalated steps run this way rather than through ``sh -c``, the way runspec
    0.70.0's env prelude does, so a sudoers rule that refuses shells for the
    run-as account still lets the console provision as it."""
    return [python, "-I", "-S", "-c", script, *args]


def preflight_argv(python: str, dest: str) -> list[str]:
    return _py_argv(python, PREFLIGHT_SCRIPT, dest)


def parse_preflight(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.rstrip("\r").split("\t", 2)
        if len(parts) == 3 and parts[0] == PREFLIGHT_MARK:
            out[parts[1]] = parts[2]
    return out


_DEST_BLOCKERS = {
    "symlink": "The location is a symlink; pick the real directory.",
    "file": "The location is a file, not a directory.",
    "notempty": "The location already exists and isn't empty.",
}


def preflight_verdict(
    facts: dict[str, str], *, require_runspec: bool = True, root_via: str = ""
) -> tuple[list[str], list[str]]:
    """``(blockers, warnings)`` from parsed preflight facts. ``root_via`` is the
    SSH login's :func:`parse_root_check` answer (``""`` = not asked): with
    ``root``/``sudo`` an unwritable parent becomes a warning, because the venv
    folder is made by root and handed to the run-as account."""
    blockers: list[str] = []
    warnings: list[str] = []
    if not facts.get("user"):
        blockers.append(
            "Couldn't run as the chosen account (is passwordless sudo/su set up for it?)."
        )
        return blockers, warnings
    version = facts.get("python_version", "")
    if not version:
        blockers.append("The chosen Python doesn't run on this host.")
    elif require_runspec and not version_ok_for_runspec(version):
        blockers.append(
            f"Python {version} is too old: runspec needs "
            f"{MIN_RUNSPEC_PYTHON[0]}.{MIN_RUNSPEC_PYTHON[1]} or newer."
        )
    if version and facts.get("venv") != "ok":
        blockers.append(
            "This Python has no venv/ensurepip module (on Debian/Ubuntu install "
            f"python{'.'.join(version.split('.')[:2])}-venv)."
        )
    dest = facts.get("dest", "")
    if dest in _DEST_BLOCKERS:
        blockers.append(_DEST_BLOCKERS[dest])
    elif dest == "logs_only":
        warnings.append(
            "The location holds only a logs/ folder (from a kept-logs deprovision); "
            "the new venv will be built around it."
        )
    if facts.get("writable") != "yes":
        where = facts.get("ancestor") or "The location"
        who = facts.get("user") or "the chosen account"
        if needs_handoff(facts) and root_via in ("root", "sudo"):
            warnings.append(
                f"{who} can't write {where}, so the console will create the venv "
                f"folder as root and hand it to {who}."
            )
        else:
            hint = (
                " Your SSH login can't sudo without a password either, so create "
                f"the folder as root and chown it to {who}, or pick a location "
                f"{who} can write."
                if needs_handoff(facts) and root_via == "no"
                else ""
            )
            blockers.append(f"{where} isn't writable by {who}.{hint}")
    return blockers, warnings


def needs_handoff(facts: dict[str, str]) -> bool:
    """True when the run-as account can't write where the venv goes but root
    could make the (absent or empty) venv folder and hand it over. A folder that
    already holds something (a kept ``logs/``, other files) is never chowned."""
    return (
        bool(facts.get("user"))
        and facts.get("writable") != "yes"
        and facts.get("dest") in ("absent", "empty")
    )


# ``sh -c ROOT_CHECK_SCRIPT sh`` as the SSH login: can it act as root without a
# password? ``root`` (logged in as root), ``sudo`` (``sudo -n`` works) or ``no``.
ROOT_CHECK_SCRIPT = r"""
if [ "$(id -u 2>/dev/null)" = 0 ]; then echo root
elif command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then echo sudo
else echo no
fi
"""


def root_check_argv() -> list[str]:
    return ["sh", "-c", ROOT_CHECK_SCRIPT, "sh"]


def parse_root_check(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        word = line.strip()
        if word in ("root", "sudo", "no"):
            return word
    return "no"


# ``<python> -I -S -c HANDOFF_SCRIPT <dest> <owner>`` as root: make the venv
# folder (and any missing parents, root-owned 755) and give just that folder to
# the owner, so the rest of the build runs as the owner. Refuses a symlink, a
# non-directory and a folder that already holds anything — it only ever chowns
# an empty dir.
HANDOFF_SCRIPT = r"""
import os, sys
os.chdir("/")
dest, owner = sys.argv[1], sys.argv[2]
def refuse(msg):
    print("refused: " + msg)
    sys.exit(64)
if not dest.startswith("/"):
    refuse(dest + " is not an absolute path")
if os.path.islink(dest):
    refuse(dest + " is a symlink")
if os.path.exists(dest):
    if not os.path.isdir(dest):
        refuse(dest + " is not a directory")
    if os.listdir(dest):
        refuse(dest + " is not empty")
import pwd
try:
    pw = pwd.getpwnam(owner)
except KeyError:
    refuse("no such user: " + owner)
os.umask(0o022)
if not os.path.isdir(dest):
    os.makedirs(dest)
if os.path.islink(dest):
    refuse(dest + " is a symlink")
os.chown(dest, pw.pw_uid, pw.pw_gid)
os.chmod(dest, 0o755)
print("created %s for %s" % (dest, owner))
"""


# Shared by CLEANUP_SCRIPT and REMOVE_SCRIPT: ``rm -rf`` of one entry, never
# following a symlink (a link is unlinked, not its target emptied).
_RM_PY = r"""
import shutil, stat
def rm(p):
    if stat.S_ISDIR(os.lstat(p).st_mode):
        shutil.rmtree(p)
    else:
        os.unlink(p)
def rm_children(d, keep=()):
    for n in os.listdir(d):
        if n not in keep:
            rm(os.path.join(d, n))
"""

# Shared guard: an absolute, normalised, non-symlink directory that resolves to
# itself (no symlinked parent). The script defines ``refuse`` first.
_DIR_GUARD_PY = r"""
if not d.startswith("/"):
    refuse(d + " is not an absolute path")
if d.endswith("/"):
    refuse("trailing slash")
if os.path.islink(d):
    refuse(d + " is a symlink")
"""
_DIR_RESOLVE_PY = r"""
if not os.path.isdir(d):
    refuse(d + " is not a directory")
real = os.path.realpath(d)
if real != d:
    refuse("%s resolves to %s" % (d, real))
"""


# ``<python> -I -S -c CLEANUP_SCRIPT <dest> <mode>`` after a failed provision,
# as the account that made the folder (root when the folder step ran, else the
# run-as account). Undoes only what this provision made, from the preflight's
# view of the folder: ``all`` (it was absent) removes the folder, ``folder`` (it
# was empty) empties it, ``logs`` (only a kept logs/) removes everything but
# logs/. Missing parents that the create step made stay. Re-checks the path on
# the host.
CLEANUP_SCRIPT = (
    r"""
import os, sys
os.chdir("/")
d, mode = sys.argv[1], sys.argv[2]
def refuse(msg):
    print("refused: " + msg)
    sys.exit(64)
"""
    + _DIR_GUARD_PY
    + r"""
if not os.path.lexists(d):
    print("nothing to remove at " + d)
    sys.exit(0)
"""
    + _DIR_RESOLVE_PY
    + _RM_PY
    + r"""
if mode == "all":
    rm(d)
    print("removed " + d)
elif mode == "folder":
    rm_children(d)
    print("emptied " + d)
elif mode == "logs":
    rm_children(d, keep=("logs",))
    print("removed the venv from %s (kept %s/logs)" % (d, d))
else:
    refuse("unknown mode " + mode)
"""
)

_CLEANUP_MODES = {"absent": "all", "empty": "folder", "logs_only": "logs"}


def cleanup_argv(python: str, dest: str, before: str) -> list[str]:
    """Undo a failed provision at ``dest`` (run on ``python``, the interpreter
    the venv was being built from). ``before`` is the preflight's ``dest``
    fact; any other state wasn't ours to build in, so it raises."""
    mode = _CLEANUP_MODES.get(before)
    if not mode:
        raise ProvisionError(f"Not cleaning up {dest}: it held {before!r} before.")
    check_removable(dest)
    return _py_argv(python, CLEANUP_SCRIPT, dest, mode)


def handoff_argv(python: str, dest: str, owner: str) -> list[str]:
    owner = validate_user(owner)
    if not owner:
        raise ProvisionError("No account to hand the venv folder to.")
    return _py_argv(python, HANDOFF_SCRIPT, dest, owner)


# --------------------------------------------------------------------------- #
# Create / install / inbox / verify
# --------------------------------------------------------------------------- #

# ``<python> -I -S -c CREATE_SCRIPT <python> <dest>`` as the run-as account.
# The recipe the removed create-venv runnable used: always --symlinks (via the
# interpreter path as given, so the venv's home matches it), then the tree is
# normalised to u=rwX,go=rX (sudo -n -u <svc> often lands at umask 027, which
# would leave the venv 750 and lock other consoles out). A kept logs/ folder is
# pruned from the chmod so its inbox keeps its mode, and symlinks are skipped:
# chmod on bin/python would follow it to the system interpreter (EPERM for a
# service account, and never ours to change; ``chmod -R`` skips them too).
# ``logs share --inbox`` (a later step) sets up / repairs logs/ itself.
CREATE_SCRIPT = r"""
import os, stat, subprocess, sys
os.chdir("/")
py, dest = sys.argv[1], sys.argv[2]
os.umask(0o022)
if not os.path.isdir(dest):
    os.makedirs(dest)
rc = subprocess.call([py, "-m", "venv", "--symlinks", dest])
if rc:
    sys.exit(rc)
def norm(p):
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        return
    is_dir = stat.S_ISDIR(st.st_mode)
    x = 0o111 if is_dir or st.st_mode & 0o111 else 0
    keep = st.st_mode & 0o7000 if is_dir else 0
    os.chmod(p, keep | 0o644 | x)
norm(dest)
for top, dirs, files in os.walk(dest):
    if top == dest and "logs" in dirs:
        dirs.remove("logs")
    for n in dirs + files:
        norm(os.path.join(top, n))
logs = os.path.join(dest, "logs")
if not os.path.isdir(logs):
    os.mkdir(logs)
print("created " + dest)
"""


def create_argv(python: str, dest: str) -> list[str]:
    return _py_argv(python, CREATE_SCRIPT, python, dest)


def pip_upgrade_argv(dest: str) -> list[str]:
    return [
        venv_paths(dest)["python"],
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "-U",
        "pip",
    ]


def install_specs(packages: list[str] | None) -> list[str]:
    """``runspec`` first, then the operator's extra packages, de-duplicated.
    Each spec is validated by the caller (``marketplace.validate_spec``)."""
    out = ["runspec"]
    for p in packages or []:
        s = str(p or "").strip()
        if not s:
            continue
        base = re.split(r"[\[<>=!~ ;]", s, maxsplit=1)[0].lower().replace("_", "-")
        if base == "runspec" and s.lower() == "runspec":
            continue
        if base == "runspec":
            out[0] = s  # a pinned runspec replaces the bare one
            continue
        if s not in out:
            out.append(s)
    return out


def inbox_argv(dest: str, acl_user: str = "") -> list[str]:
    """``logs share --inbox`` as the owner; ``acl_user`` (the SSH login, when it
    isn't the owner) also gets a POSIX ACL on logs/ so it can read the runs
    index."""
    argv = [venv_paths(dest)["runspec"], "logs", "share", "--inbox"]
    if acl_user:
        argv += ["--acl-user", acl_user]
    return argv


def verify_argv(dest: str) -> list[str]:
    return [venv_paths(dest)["runspec"], "--version"]


# --------------------------------------------------------------------------- #
# Host-side audit events (runspec 0.71.0+ ledger, OS-log fallback)
# --------------------------------------------------------------------------- #


def _audit_pairs(flag: str, fields: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k, v in fields.items():
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (list, tuple)):
            v = " ".join(str(x) for x in v)
        out += [flag, f"{k}={v}"]
    return out


def audit_record_argv(
    dest: str,
    event: str,
    outcome: str,
    *,
    target: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> list[str]:
    """``<venv>/bin/runspec audit record …``, run on the host as the venv owner:
    the event goes to the venv's hash-chained ledger (``logs/audit.jsonl``) and
    the host's syslog, the way any runspec control-plane change is recorded.
    Needs runspec 0.71.0+ in the venv; the caller falls back to
    :func:`audit_logger_argv` when it fails."""
    return [
        venv_paths(dest)["runspec"],
        "audit",
        "record",
        event,
        "--outcome",
        outcome,
        *_audit_pairs("--target", target or {}),
        *_audit_pairs("--detail", detail or {}),
    ]


def audit_logger_argv(
    event: str,
    outcome: str,
    *,
    target: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> list[str]:
    """The OS-log-only fallback: one ``logger`` line on the host's syslog (the
    same ``auth`` facility runspec's ledger mirrors to), for a venv whose
    runspec predates ``runspec audit``."""
    body = {
        "event": event,
        "outcome": outcome,
        "target": {k: v for k, v in (target or {}).items() if v not in (None, "")},
        "detail": {k: v for k, v in (detail or {}).items() if v not in (None, "")},
    }
    return [
        "logger",
        "-t",
        "runspec",
        "-p",
        "auth.notice",
        "runspec-audit " + json.dumps(body, sort_keys=True, default=str),
    ]


# --------------------------------------------------------------------------- #
# Deprovision
# --------------------------------------------------------------------------- #

INSPECT_MARK = "RSDP"

# ``sh -c INSPECT_SCRIPT sh <root>`` — read-only facts for the confirm dialog.
INSPECT_SCRIPT = r"""
cd / 2>/dev/null
r=$1
out() { printf 'RSDP\t%s\t%s\n' "$1" "$2"; }
if [ -L "$r" ]; then out kind symlink
elif [ ! -d "$r" ]; then out kind missing
elif [ -f "$r/pyvenv.cfg" ] && [ -e "$r/bin/runspec" ]; then out kind venv
else out kind other
fi
out owner "$(stat -c %U "$r" 2>/dev/null || stat -f %Su "$r" 2>/dev/null)"
out user "$(id -un 2>/dev/null)"
if [ -x "$r/bin/python" ]; then out python "$r/bin/python"
else out python "$(command -v python3 2>/dev/null)"
fi
out size_kb "$(du -sk "$r" 2>/dev/null | cut -f1)"
if [ -d "$r/logs" ]; then out logs_kb "$(du -sk "$r/logs" 2>/dev/null | cut -f1)"; else out logs_kb 0; fi
"""

# ``<python> -I -S -c REMOVE_SCRIPT <root> <keep_logs 1|0>`` as the venv owner,
# on the venv's own ``bin/python`` (the inspect step's ``python`` fact). Re-checks
# every guard on the host in the same command as the delete, so the check and
# the rm can't diverge (a swapped-in symlink, a path that stopped being a venv).
REMOVE_SCRIPT = (
    r"""
import os, sys
os.chdir("/")
d, keep = sys.argv[1], sys.argv[2]
def refuse(msg):
    print("refused: " + msg)
    sys.exit(64)
"""
    + _DIR_GUARD_PY
    + r"""
if not os.path.isdir(d):
    refuse(d + " does not exist")
"""
    + _DIR_RESOLVE_PY
    + r"""
if not os.path.isfile(os.path.join(d, "pyvenv.cfg")):
    refuse(d + " has no pyvenv.cfg (not a venv)")
if not os.path.exists(os.path.join(d, "bin", "runspec")):
    refuse(d + " has no bin/runspec")
homes = set([os.environ.get("HOME", "")])
try:
    import pwd
    homes.add(pwd.getpwuid(os.geteuid()).pw_dir)
except (ImportError, KeyError):
    pass
if d in homes:
    refuse(d + " is the home directory")
"""
    + _RM_PY
    + r"""
if keep == "1":
    rm_children(d, keep=("logs",))
    print("removed %s (kept %s/logs)" % (d, d))
else:
    rm(d)
    print("removed " + d)
"""
)


def inspect_argv(root: str) -> list[str]:
    return ["sh", "-c", INSPECT_SCRIPT, "sh", root]


def parse_inspect(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.rstrip("\r").split("\t", 2)
        if len(parts) == 3 and parts[0] == INSPECT_MARK:
            out[parts[1]] = parts[2]
    return out


def check_removable(root: str) -> str:
    """The normalised venv root if it may be deleted, else :class:`ProvisionError`."""
    norm = normalize_dest(root)
    if norm != root.rstrip("/"):
        raise ProvisionError(f"{root} is not a normalised path.")
    return norm


def remove_argv(python: str, root: str, *, keep_logs: bool) -> list[str]:
    """Delete the venv at ``root`` on ``python`` — the venv's own interpreter
    (from :func:`parse_inspect`), which a sudoers rule scoped to the venv's
    ``bin/`` also allows."""
    check_removable(root)
    if not (python or "").startswith("/"):
        raise ProvisionError(f"No Python found on the host to remove {root} with.")
    return _py_argv(python, REMOVE_SCRIPT, root, "1" if keep_logs else "0")


def venv_name(root: str) -> str:
    return posixpath.basename(root.rstrip("/"))


# --------------------------------------------------------------------------- #
# Host-file edits + audit
# --------------------------------------------------------------------------- #


def add_runspec_path(entry: dict[str, Any], runspec_path: str) -> bool:
    """Append ``runspec_path`` to a host entry's ``runspec_paths`` (migrating a
    legacy ``runspec_path``). Returns whether the entry changed."""
    paths = list(entry.get("runspec_paths") or [])
    if not paths and entry.get("runspec_path"):
        paths = [str(entry["runspec_path"])]
    if runspec_path in paths:
        return False
    entry["runspec_paths"] = [*paths, runspec_path]
    entry.pop("runspec_path", None)
    return True


def remove_runspec_path(entry: dict[str, Any], root: str) -> bool:
    """Drop every ``runspec_paths`` entry inside ``root``. Returns whether the
    entry changed."""
    paths = list(entry.get("runspec_paths") or [])
    if not paths and entry.get("runspec_path"):
        paths = [str(entry["runspec_path"])]
    keep = []
    for p in paths:
        try:
            r = root_from_runspec_path(p)
        except ProvisionError:
            keep.append(p)
            continue
        if r != root:
            keep.append(p)
    if keep == paths:
        return False
    entry.pop("runspec_path", None)
    if keep:
        entry["runspec_paths"] = keep
    else:
        entry.pop("runspec_paths", None)
    return True


def audit_record(event: str, ts: str, **fields: Any) -> dict[str, Any]:
    """One audit line for the console's audit log (the same shape as the
    disclaimer acknowledgement). ``event`` is one of ``venv.provision.started``
    / ``.succeeded`` / ``.failed``, ``venv.deprovision.confirmed`` /
    ``.succeeded`` / ``.failed``, ``venv.logs.retained`` — kept stable so an
    append-only audit sink can pick them up later."""
    detail = {k: v for k, v in fields.items() if v not in (None, "")}
    host = detail.get("host", "")
    path = detail.get("path", "")
    level = "WARNING" if event.endswith(".failed") else "INFO"
    return {
        "ts": ts,
        "level": level,
        "message": f"{event} {host}:{path}".strip(),
        "extra": {"event": event, **detail},
    }


def tail(text: str, limit: int = 12) -> str:
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[-limit:])


# --------------------------------------------------------------------------- #
# Failure text
# --------------------------------------------------------------------------- #

# Lines of the scripts this module sends. sudo's refusal message quotes the
# whole command it refused (``Sorry, user x is not allowed to execute '/bin/sh
# -c <script> …' as y``), so a raw tail of the output is the end of a script,
# not an error. These lines are dropped from any failure text.
_SCRIPT_LINES = frozenset(
    ln.strip()
    for s in (
        PROBE_SCRIPT,
        PREFLIGHT_SCRIPT,
        ROOT_CHECK_SCRIPT,
        HANDOFF_SCRIPT,
        CLEANUP_SCRIPT,
        CREATE_SCRIPT,
        INSPECT_SCRIPT,
        REMOVE_SCRIPT,
    )
    for ln in s.splitlines()
    if ln.strip()
)


def _echoes_script(line: str) -> bool:
    return line.strip() in _SCRIPT_LINES


def _refused_program(text: str) -> str:
    """The program sudo refused, from ``… not allowed to execute '<prog> …'``
    (``"that command"`` when it can't be read)."""
    m = re.search(r"not allowed to execute '([^' \t\n]+)", text or "")
    return m.group(1) if m else "that command"


def failure_text(
    text: str,
    code: int,
    *,
    run_as: str = "",
    become_method: str = "sudo",
    limit: int = 6,
) -> str:
    """What to show for a host command that failed: a plain sentence when the
    escalation to ``run_as`` was refused (sudo/su), else the tail of the output
    with any echoed script lines removed, else ``exit N``."""
    raw = text or ""
    low = raw.lower()
    who = run_as or "that account"
    method = (become_method or "sudo").strip().lower()
    if run_as and method != "su":
        if "is not allowed to execute" in low:
            prog = _refused_program(raw)
            return (
                f"sudo refused: the SSH login isn't allowed to run {prog} as "
                f"{who} (provisioning runs the chosen Python as the run-as "
                f"account). Allow it in sudoers, or set Run as to an account "
                f"the login can use."
            )
        if "not in the sudoers file" in low or "may not run sudo" in low:
            return (
                f"sudo refused: the SSH login can't use sudo, so it "
                f"can't act as {who}. Leave Run as empty to provision as the "
                f"SSH login itself."
            )
        if "a password is required" in low or "a terminal is required" in low:
            return (
                f"sudo needs a password to act as {who}; the console only uses "
                f"passwordless sudo (sudo -n)."
            )
    if run_as and method == "su":
        if "authentication failure" in low or "must be run from a terminal" in low:
            return (
                f"su needs a password to act as {who}; the console can't type "
                f"one. Use passwordless sudo instead."
            )
    lines = [
        ln.rstrip() for ln in raw.splitlines() if ln.strip() and not _echoes_script(ln)
    ]
    return "\n".join(lines[-limit:]) or f"exit {code}"

"""Write config files as root, but only where a root-owned allowlist says.

Backs the ``config-file`` runnable in ``runspec-linux-root``: the narrow, root
version of ``filesystem write-file``/``edit-file``. It writes, edits and reads
regular text files and nothing else (no move, delete, list or search), and only
at paths matched by the venv's allowlist file::

    # {venv}/runspec_root_files.conf  (owned by root, not group/other-writable)
    /etc/alloy/*
    /etc/systemd/system/alloy.service.d/*

One pattern per line (``#`` starts a comment). A pattern is an absolute path where ``*`` matches within one directory level,
``**`` matches any number of levels and ``?`` one character. No allowlist file
means an empty one: everything is refused, and the error names the file.

On top of the allowlist:

* The path is resolved through its parent directory (``realpath``) and the
  resolved path is what must match, so ``..`` or a symlinked directory can't
  reach outside it. The file itself must not be a symlink, and must be a regular
  file when it exists.
* A few files are never written whatever the allowlist says (:data:`DENIED`):
  accounts, sudoers, PAM, the SSH server and root's SSH keys.
* The write is atomic (a temp file in the same directory, ``fsync``, rename). An
  existing file keeps its owner, group and mode unless they are given; a new one
  is ``root:root`` (the caller's account) ``0644``, or ``0600`` when ``private``.
* The old content is copied to the venv's backup directory first (owner-only,
  the newest few per file kept), never next to the file, where a service that
  loads a whole directory (``/etc/alloy``, a systemd drop-in folder) would read
  it.

Pure stdlib, no runspec dependency. Secret placeholders are filled by the
wrapper (it owns the secret channel); a function here gets the final text and
returns a diff the wrapper masks before printing.
"""

from __future__ import annotations

import difflib
import grp
import os
import pwd
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from runspec_linux_core.errors import LinuxCoreError

#: The allowlist file's name, in the venv root.
ALLOWLIST_NAME = "runspec_root_files.conf"

#: Where backups go, in the venv root.
BACKUP_DIR_NAME = "root_file_backups"

#: Backups kept per file.
BACKUPS_KEPT = 5

#: Largest file read or written.
MAX_BYTES = 1024 * 1024

#: Never written or read, whatever the allowlist says.
DENIED = (
    "/etc/passwd",
    "/etc/passwd-",
    "/etc/shadow*",
    "/etc/group",
    "/etc/group-",
    "/etc/gshadow*",
    "/etc/sudoers",
    "/etc/sudoers.d/**",
    "/etc/pam.d/**",
    "/etc/security/**",
    "/etc/ssh/**",
    "/root/.ssh/**",
    "/etc/runspec*",
)


class RootFileError(LinuxCoreError):
    """A config-file request was refused or failed. Nothing was changed."""


# ── allowlist ────────────────────────────────────────────────────────────────


def _pattern_re(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, pattern: str) -> bool:
    """True when the absolute ``path`` matches ``pattern`` (``*`` one level,
    ``**`` any levels, ``?`` one character)."""
    return _pattern_re(pattern).match(path) is not None


def check_pattern(pattern: str) -> str:
    """Validate an allowlist pattern; returns it stripped or raises."""
    p = pattern.strip()
    if not p.startswith("/"):
        raise RootFileError(f"not an absolute path pattern: {pattern!r}")
    parts = p.split("/")[1:]
    if any(ch.isspace() for ch in p) or "#" in p:
        raise RootFileError(f"pattern has a space or '#': {pattern!r}")
    if any(part in ("", ".", "..") for part in parts[:-1]) or parts[-1] in (".", ".."):
        raise RootFileError(f"pattern has an empty, '.' or '..' part: {pattern!r}")
    if not parts or any(ch in parts[0] for ch in "*?"):
        raise RootFileError(f"pattern must start with a fixed directory, e.g. /etc/alloy/*: {pattern!r}")
    if len(parts) < 2:
        raise RootFileError(f"pattern names a top-level folder only; name a folder inside it: {pattern!r}")
    return p


def _check_trusted(path: Path, owner_uid: int) -> None:
    st = path.stat()
    if st.st_uid != owner_uid or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RootFileError(f"{path} must be owned by root and not writable by group or others (it decides where root may write); fix its owner/mode first. Nothing was changed.")


def read_allowlist(path: Path, *, owner_uid: int = 0) -> list[str]:
    """The patterns in the allowlist file; ``[]`` when it doesn't exist.
    Refuses a file (or folder holding it) that isn't root's alone."""
    if not path.exists():
        return []
    _check_trusted(path.parent, owner_uid)
    _check_trusted(path, owner_uid)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RootFileError(f"can't read {path}: {exc}") from exc
    out: list[str] = []
    for n, line in enumerate(text.splitlines(), start=1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            out.append(check_pattern(line))
        except RootFileError as exc:
            raise RootFileError(f"{path} line {n}: {exc}") from None
    return out


_HEADER = (
    "# Paths config-file (runspec-linux-root) may write as root, one per line.\n"
    "# '*' matches within one folder, '**' any depth, '?' one character.\n"
    "# Edit with `config-file allow` / `config-file disallow`, or by hand as root.\n"
)


def _allowlist_text(patterns: list[str]) -> str:
    return _HEADER + "".join(f"{p}\n" for p in patterns)


def _write_allowlist(path: Path, patterns: list[str]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".runspec_root_files.conf.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_allowlist_text(patterns))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def allowed(allowlist: Path, *, owner_uid: int = 0) -> dict[str, Any]:
    """What may be written: the allowlist file, its patterns, the fixed denials."""
    return {
        "allowlist": str(allowlist),
        "exists": allowlist.exists(),
        "paths": read_allowlist(allowlist, owner_uid=owner_uid),
        "never": list(DENIED),
    }


def allow(allowlist: Path, pattern: str, *, owner_uid: int = 0) -> dict[str, Any]:
    """Add a pattern to the allowlist (creating the file). Same pattern twice
    changes nothing."""
    p = check_pattern(pattern)
    current = read_allowlist(allowlist, owner_uid=owner_uid)
    if allowlist.parent.exists():
        _check_trusted(allowlist.parent, owner_uid)
    if p in current:
        return {"allowlist": str(allowlist), "paths": current, "changed": False}
    current.append(p)
    _write_allowlist(allowlist, current)
    return {"allowlist": str(allowlist), "paths": current, "changed": True}


def disallow(allowlist: Path, pattern: str, *, owner_uid: int = 0) -> dict[str, Any]:
    """Remove a pattern from the allowlist. Removing one that isn't there is
    not an error."""
    p = pattern.strip()
    current = read_allowlist(allowlist, owner_uid=owner_uid)
    if p not in current:
        return {"allowlist": str(allowlist), "paths": current, "changed": False}
    current.remove(p)
    _write_allowlist(allowlist, current)
    return {"allowlist": str(allowlist), "paths": current, "changed": True}


# ── resolving a target ───────────────────────────────────────────────────────


def _check_denied(path: str) -> None:
    for pattern in DENIED:
        if matches(path, pattern):
            raise RootFileError(f"{path} is never written by config-file (matches {pattern}). Nothing was changed.")


def resolve_target(
    path: str,
    allowlist: Path,
    *,
    owner_uid: int = 0,
    make_dirs: bool = False,
) -> Path:
    """The real path ``path`` names, after checking it may be touched.

    The parent folder is resolved (``realpath``) and the file name added back;
    that resolved path must match an allowlist pattern and none of
    :data:`DENIED`. A symlink or non-regular file at the path is refused. A
    missing parent folder is refused unless ``make_dirs`` (then the nearest
    existing ancestor is resolved instead)."""
    if not path or not path.startswith("/"):
        raise RootFileError(f"give an absolute path: {path!r}")
    given = Path(path)
    if given.name in ("", ".", ".."):
        raise RootFileError(f"not a file path: {path!r}")
    _check_denied(os.path.normpath(path))
    parent = given.parent
    missing: list[str] = []
    while not parent.exists():
        if not make_dirs:
            raise RootFileError(f"folder {given.parent} doesn't exist (pass --make-dirs to create it). Nothing was changed.")
        missing.insert(0, parent.name)
        parent = parent.parent
    real = Path(os.path.realpath(parent)).joinpath(*missing, given.name)
    target = str(real)
    _check_denied(target)
    patterns = read_allowlist(allowlist, owner_uid=owner_uid)
    if not any(matches(target, p) for p in patterns):
        where = f" (resolved from {path})" if target != os.path.normpath(path) else ""
        hint = f"Allowed: {', '.join(patterns)}." if patterns else "Nothing is allowed yet."
        raise RootFileError(f"{target}{where} isn't in the allowlist {allowlist}. {hint} A person adds a path with `config-file allow '<pattern>'` (it can't be run by an agent). Nothing was changed.")
    if os.path.lexists(target):
        st = os.lstat(target)
        if stat.S_ISLNK(st.st_mode):
            raise RootFileError(f"{target} is a symlink; config-file writes regular files only. Nothing was changed.")
        if not stat.S_ISREG(st.st_mode):
            raise RootFileError(f"{target} isn't a regular file. Nothing was changed.")
    return real


# ── ownership / mode ─────────────────────────────────────────────────────────


def parse_mode(mode: str | None) -> int | None:
    if mode is None or mode == "":
        return None
    if not re.fullmatch(r"0?[0-7]{3,4}", mode):
        raise RootFileError(f"mode must be octal like 0644 or 0640: {mode!r}")
    value = int(mode, 8)
    if value & 0o7000:
        raise RootFileError(f"setuid/setgid/sticky bits aren't allowed: {mode!r}")
    return value


def _uid(owner: str | None) -> int | None:
    if not owner:
        return None
    try:
        return pwd.getpwnam(owner).pw_uid
    except KeyError:
        raise RootFileError(f"no such user: {owner!r}") from None


def _gid(group: str | None) -> int | None:
    if not group:
        return None
    try:
        return grp.getgrnam(group).gr_gid
    except KeyError:
        raise RootFileError(f"no such group: {group!r}") from None


def _owner_names(uid: int, gid: int) -> tuple[str, str]:
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    try:
        group = grp.getgrgid(gid).gr_name
    except KeyError:
        group = str(gid)
    return user, group


# ── read / write / edit ──────────────────────────────────────────────────────


def _read_text(target: Path) -> str:
    size = target.stat().st_size
    if size > MAX_BYTES:
        raise RootFileError(f"{target} is {size} bytes; config-file handles files up to {MAX_BYTES}.")
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise RootFileError(f"{target} isn't UTF-8 text.") from None


def read_file(path: str, allowlist: Path, *, owner_uid: int = 0) -> dict[str, Any]:
    """The text of an allowlisted file, with its owner, group and mode."""
    target = resolve_target(path, allowlist, owner_uid=owner_uid)
    if not target.exists():
        raise RootFileError(f"{target} doesn't exist.")
    st = target.stat()
    user, group = _owner_names(st.st_uid, st.st_gid)
    return {
        "path": str(target),
        "content": _read_text(target),
        "owner": user,
        "group": group,
        "mode": f"{stat.S_IMODE(st.st_mode):04o}",
    }


def _backup(target: Path, backup_dir: Path) -> str:
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    flat = str(target).strip("/").replace("/", "__")
    stamp = time.strftime("%Y%m%dT%H%M%S")
    dest = backup_dir / f"{flat}.{stamp}"
    n = 1
    while dest.exists():
        n += 1
        dest = backup_dir / f"{flat}.{stamp}.{n}"
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(target.read_bytes())
    kept = sorted(backup_dir.glob(f"{flat}.*"), key=lambda p: p.stat().st_mtime)
    for old in kept[:-BACKUPS_KEPT]:
        old.unlink(missing_ok=True)
    return str(dest)


def _diff(target: Path, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a{target}",
            tofile=f"b{target}",
        )
    )


def _apply(
    target: Path,
    content: str,
    *,
    owner: str | None,
    group: str | None,
    mode: str | None,
    private: bool,
    backup_dir: Path,
    dry_run: bool,
) -> dict[str, Any]:
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise RootFileError(f"content is over {MAX_BYTES} bytes.")
    want_mode = parse_mode(mode)
    want_uid = _uid(owner)
    want_gid = _gid(group)
    exists = target.exists()
    if exists:
        st = target.stat()
        old = _read_text(target)
        cur_uid, cur_gid, cur_mode = st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)
    else:
        old = ""
        cur_uid, cur_gid = os.geteuid(), os.getegid()
        cur_mode = 0o600 if private else 0o644
    uid = cur_uid if want_uid is None else want_uid
    gid = cur_gid if want_gid is None else want_gid
    new_mode = cur_mode if want_mode is None else want_mode
    changed = (not exists) or old != content or (uid, gid, new_mode) != (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))
    user, grp_name = _owner_names(uid, gid)
    result: dict[str, Any] = {
        "path": str(target),
        "created": not exists,
        "changed": changed,
        "dry_run": dry_run,
        "owner": user,
        "group": grp_name,
        "mode": f"{new_mode:04o}",
        "diff": _diff(target, old, content),
    }
    if private and new_mode & 0o004:
        result["warning"] = f"{target} holds a secret and every account can read it (mode {new_mode:04o})."
    if dry_run or not changed:
        return result
    if exists:
        result["backup"] = _backup(target, backup_dir)
    else:
        _make_dirs(target.parent)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.")
    try:
        os.fchmod(fd, new_mode)
        if (uid, gid) != (os.geteuid(), os.getegid()):
            os.fchown(fd, uid, gid)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return result


def _make_dirs(folder: Path) -> None:
    """Create missing folders root-owned ``0755`` (set explicitly: a sudo'd
    process may run with umask 027)."""
    missing: list[Path] = []
    while not folder.exists():
        missing.insert(0, folder)
        folder = folder.parent
    for d in missing:
        d.mkdir(mode=0o755)
        os.chmod(d, 0o755)


def write_file(
    path: str,
    content: str,
    allowlist: Path,
    *,
    backup_dir: Path,
    owner: str | None = None,
    group: str | None = None,
    mode: str | None = None,
    private: bool = False,
    make_dirs: bool = False,
    dry_run: bool = False,
    owner_uid: int = 0,
) -> dict[str, Any]:
    """Create or replace an allowlisted file with ``content``.

    Returns ``{path, created, changed, dry_run, owner, group, mode, diff,
    backup?, warning?}``. The same content, owner and mode again changes
    nothing (no backup, no write)."""
    target = resolve_target(path, allowlist, owner_uid=owner_uid, make_dirs=make_dirs)
    return _apply(
        target,
        content,
        owner=owner,
        group=group,
        mode=mode,
        private=private,
        backup_dir=backup_dir,
        dry_run=dry_run,
    )


def edit_file(
    path: str,
    edits: list[tuple[str, str]],
    allowlist: Path,
    *,
    backup_dir: Path,
    owner: str | None = None,
    group: str | None = None,
    mode: str | None = None,
    private: bool = False,
    dry_run: bool = False,
    owner_uid: int = 0,
) -> dict[str, Any]:
    """Replace text in an existing allowlisted file: each ``(old, new)`` pair in
    order, and each ``old`` must appear exactly once (so an edit can't land in
    the wrong place). Nothing is written when any pair doesn't apply."""
    target = resolve_target(path, allowlist, owner_uid=owner_uid)
    if not target.exists():
        raise RootFileError(f"{target} doesn't exist; use write to create it.")
    if not edits:
        raise RootFileError("give at least one --old-text / --new-text pair.")
    text = _read_text(target)
    for i, (old, new) in enumerate(edits, start=1):
        if not old:
            raise RootFileError(f"edit #{i}: old text is empty.")
        count = text.count(old)
        if count != 1:
            where = "isn't in the file" if count == 0 else f"appears {count} times; include more of the line"
            raise RootFileError(f"edit #{i}: the old text {where}. Nothing was changed.")
        text = text.replace(old, new, 1)
    result = _apply(
        target,
        text,
        owner=owner,
        group=group,
        mode=mode,
        private=private,
        backup_dir=backup_dir,
        dry_run=dry_run,
    )
    result["edits_applied"] = len(edits)
    return result

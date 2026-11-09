"""fs_browse.py — directory listing for the Forms path-picker.

Powers the file/directory browser that backs ``path`` form fields. Increment 1 is
**login-user identity only**: local hosts list via ``os.scandir``; remote hosts
list with a single ``find -printf`` command over the host's pooled SSH transport
(reusing the same one-shot exec path as discovery / history / logs).

The listing functions here are pure and side-effect free apart from the local
filesystem read, so they unit-test without a Bridge or a live SSH connection. A
later increment adds a service-account view (the same ``find`` wrapped in the
host's ``run_as``/become) and an SFTP path shared with the file-transfer feature.

Entry shape returned to the UI::

    {"cwd": "/abs/dir", "parent": "/abs" | None,
     "entries": [{"name": str, "type": "directory" | "file",
                  "size": int, "mtime": int}],
     "truncated": bool}
"""

from __future__ import annotations

import os
import posixpath
import shlex
from typing import Any

# Cap a single listing so a giant directory can't flood the bridge / UI. Set
# comfortably above a full ``/usr/bin`` (~1k+ entries on a typical Linux box) so
# common system dirs list in full; the UI filters/caps what it actually renders.
MAX_ENTRIES = 5000

# Per child: "<type>\t<size>\t<mtime_epoch>\t<name>". ``-L`` follows symlinks so a
# symlinked directory reports its target type ('d') and stays navigable.
_FIND = "find -L . -maxdepth 1 -mindepth 1 -printf '%y\\t%s\\t%T@\\t%f\\n' 2>/dev/null"


def remote_list_command(
    path: str,
    run_as: str = "",
    become_method: str = "sudo",
    become_flags: str | None = None,
) -> str:
    """Shell command that prints the resolved absolute dir, then one line per child.

    ``cd``-ing first resolves ``~``/relative paths to an absolute ``pwd`` and makes
    a missing/unreadable directory a non-zero exit the caller surfaces as an error.

    With ``run_as`` set, the whole listing runs as that user via the host's become
    method (``sh -c`` wrapped in ``sudo -u`` / ``su`` / …) — so ``$HOME`` and the
    visible files are the service account's, matching what the runnable will see.
    Reuses the core ``build_become_argv`` so escalation is identical to invocation.
    """
    dir_expr = shlex.quote(path) if path else '"$HOME"'
    inner = f"cd {dir_expr} 2>/dev/null && pwd && {_FIND}"
    if not run_as:
        return inner
    from runspec.become import build_become_argv

    argv = build_become_argv(["sh", "-c", inner], run_as, become_method, become_flags)
    return " ".join(shlex.quote(tok) for tok in argv)


def parse_remote_listing(stdout: str, dirs_only: bool = False) -> dict[str, Any]:
    """Parse :func:`remote_list_command` output into the entry payload."""
    lines = stdout.splitlines()
    if not lines:
        return {"cwd": "", "parent": None, "entries": [], "truncated": False}
    cwd = lines[0].rstrip("/") or "/"
    entries: list[dict[str, Any]] = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        type_char, size_s, mtime_s, name = parts
        is_dir = type_char == "d"
        if dirs_only and not is_dir:
            continue
        entries.append(_entry(name, is_dir, _to_int(size_s), int(_to_float(mtime_s))))
    return _finish(cwd, entries, _posix_parent(cwd))


def list_local(path: str, dirs_only: bool = False) -> dict[str, Any]:
    """List a directory on the local host via ``os.scandir``."""
    base = os.path.abspath(
        os.path.expanduser(path) if path else os.path.expanduser("~")
    )
    if not os.path.isdir(base):
        raise NotADirectoryError(f"Not a directory: {base}")
    entries: list[dict[str, Any]] = []
    with os.scandir(base) as it:
        for de in it:
            try:
                is_dir = de.is_dir()  # follows symlinks
            except OSError:
                continue
            if dirs_only and not is_dir:
                continue
            try:
                st = de.stat()
                size, mtime = int(st.st_size), int(st.st_mtime)
            except OSError:
                size, mtime = 0, 0
            entries.append(_entry(de.name, is_dir, size, mtime))
    return _finish(base, entries, _local_parent(base))


def list_sftp(sftp: Any, path: str, dirs_only: bool = False) -> dict[str, Any]:
    """List a directory over an open SFTP client (ad-hoc / relay source browse).

    POSIX targets only (the source is reached via a jump). ``path`` empty means
    the connection's default directory (home). Symlinks are reported by their own
    type — not dereferenced — in this first cut.
    """
    import stat as statmod

    cwd = sftp.normalize(path if path and path.strip() else ".")
    cwd = cwd.rstrip("/") or "/"
    entries: list[dict[str, Any]] = []
    for attr in sftp.listdir_attr(cwd):
        is_dir = statmod.S_ISDIR(attr.st_mode)
        if dirs_only and not is_dir:
            continue
        entries.append(
            _entry(
                attr.filename, is_dir, int(attr.st_size or 0), int(attr.st_mtime or 0)
            )
        )
    return _finish(cwd, entries, _posix_parent(cwd))


# ── Files tab: new folder + view (read the end of a file) ────────────────────

# View shows the end of a text file: at most this many lines, and never more
# than VIEW_MAX_BYTES of it, so a 2 GB log reads as its last screenfuls.
VIEW_DEFAULT_LINES = 500
VIEW_MAX_LINES = 5000
VIEW_MAX_BYTES = 1_000_000


def valid_folder_name(name: str) -> str:
    """Return ``name`` stripped, or raise ValueError for a name that isn't one
    folder (empty, ``.``/``..``, or holding a path separator / NUL)."""
    clean = (name or "").strip()
    if not clean or clean in (".", ".."):
        raise ValueError("enter a folder name")
    if any(ch in clean for ch in ("/", "\\", "\0")):
        raise ValueError("a folder name can't contain / or \\")
    return clean


def remote_mkdir_command(parent: str, name: str) -> str:
    """Shell command that creates ``name`` inside ``parent`` and prints its path.

    ``mkdir`` without ``-p`` so an existing folder is an error, not a no-op."""
    q = shlex.quote
    return f"cd {q(parent)} && mkdir -- {q(name)} && cd -- {q(name)} && pwd"


def make_local_dir(parent: str, name: str) -> str:
    """Create ``name`` inside the local ``parent``; returns the new path."""
    base = (
        os.path.abspath(os.path.expanduser(parent))
        if parent
        else os.path.expanduser("~")
    )
    path = os.path.join(base, valid_folder_name(name))
    os.mkdir(path)
    return path


def view_lines(lines: Any) -> int:
    """Clamp a requested line count to 1..VIEW_MAX_LINES (default on junk)."""
    try:
        n = int(lines)
    except (TypeError, ValueError):
        return VIEW_DEFAULT_LINES
    return max(1, min(VIEW_MAX_LINES, n))


def remote_view_command(path: str, lines: int) -> str:
    """Shell command printing the file's size on line one, then its last
    ``lines`` lines (capped at VIEW_MAX_BYTES). A missing, unreadable or
    non-regular file exits non-zero."""
    q = shlex.quote(path)
    n = view_lines(lines)
    return (
        f"test -f {q} && test -r {q} && wc -c < {q} && "
        f"tail -n {n} -- {q} | tail -c {VIEW_MAX_BYTES}"
    )


def parse_view_output(stdout: str, lines: int) -> dict[str, Any]:
    """Turn :func:`remote_view_command` output into the view payload."""
    head, _, body = stdout.partition("\n")
    return _view_payload(_to_int(head.strip()), body, view_lines(lines))


def view_local(path: str, lines: int) -> dict[str, Any]:
    """The last ``lines`` lines of a local file (at most VIEW_MAX_BYTES read)."""
    full = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(full):
        raise FileNotFoundError(f"Not a file: {full}")
    size = os.path.getsize(full)
    with open(full, "rb") as fh:
        if size > VIEW_MAX_BYTES:
            fh.seek(size - VIEW_MAX_BYTES)
        data = fh.read(VIEW_MAX_BYTES)
    return _view_payload(
        size, data.decode("utf-8", errors="replace"), view_lines(lines)
    )


def _view_payload(size: int, text: str, lines: int) -> dict[str, Any]:
    if "\0" in text:
        return {"size": size, "binary": True, "text": "", "lines": 0, "partial": True}
    rows = text.splitlines()
    shown = rows[-lines:]
    # Partial when the file holds more than what's shown: more lines than asked
    # for, or the byte cap cut into it.
    partial = len(rows) > lines or len(text.encode("utf-8", errors="replace")) < size
    return {
        "size": size,
        "binary": False,
        "text": "\n".join(shown),
        "lines": len(shown),
        "partial": partial,
    }


# ── helpers ──────────────────────────────────────────────────────────────────


def _entry(name: str, is_dir: bool, size: int, mtime: int) -> dict[str, Any]:
    return {
        "name": name,
        "type": "directory" if is_dir else "file",
        "size": size,
        "mtime": mtime,
    }


def _finish(
    cwd: str, entries: list[dict[str, Any]], parent: str | None
) -> dict[str, Any]:
    # Directories first, then case-insensitive by name.
    entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))
    truncated = len(entries) > MAX_ENTRIES
    return {
        "cwd": cwd,
        "parent": parent,
        "entries": entries[:MAX_ENTRIES],
        "truncated": truncated,
    }


def _posix_parent(cwd: str) -> str | None:
    if not cwd or cwd == "/":
        return None
    return posixpath.dirname(cwd.rstrip("/")) or "/"


def _local_parent(base: str) -> str | None:
    parent = os.path.dirname(base)
    return None if parent == base else parent  # filesystem / drive root


def _to_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

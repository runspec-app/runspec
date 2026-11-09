"""_files_worker.py — the Files tab's file actions, run as a venv owner.

Stdlib only and Python 3.8-compatible. When a Files pane browses a host as one
of its venv owners ("Browse as"), or the Forms path picker lists as a
runnable's service account, the console sends this file's source to that
venv's own interpreter::

    sudo -n -u <owner> {venv}/bin/python -I -S -c <source>

and the request as one JSON object on standard input. No shell runs as the
owner, so a sudoers rule that only allows the venv's ``bin/`` (and refuses
shells) still covers it — the same route Env files and runnable runs use.

Request: ``{"op": ..., ...}``. The result is one JSON line after
:data:`MARKER` on stdout, ``{"ok": true, ...}`` or ``{"ok": false, "error":
...}``.

* ``list`` ``{path, dirs_only}`` — ``{cwd, entries: [[name, is_dir, size,
  mtime], ...]}``. An empty path is the account's home; symlinked folders
  list as folders.
* ``mkdir`` ``{parent, name}`` — ``{path}``. An existing folder is an error.
* ``view`` ``{path, max_bytes}`` — ``{size, text}``: at most the last
  ``max_bytes`` of the file, decoded as UTF-8.
* ``rename`` ``{parent, old, new}`` — ``{path}``. Never overwrites.
* ``delete`` ``{paths}`` — ``{deleted, failed: [{path, error}]}``: files and
  symlinks are removed, folders only when empty.
"""

import json
import os
import sys

MARKER = "=====RSFILES "


def _home():
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_dir
    except Exception:
        return os.path.expanduser("~")


def _abs(path):
    """``path`` made absolute against the account's own home (``~`` too), not
    the HOME sudo may have kept from the login."""
    p = path or ""
    if p == "~" or p.startswith("~/"):
        p = _home() + p[1:]
    if not p:
        p = _home()
    if not os.path.isabs(p):
        p = os.path.join(_home(), p)
    return os.path.normpath(p)


def _why(exc):
    if isinstance(exc, PermissionError):
        return "permission denied"
    if isinstance(exc, FileNotFoundError):
        return "no longer exists"
    if isinstance(exc, FileExistsError):
        return "already exists"
    return exc.strerror or str(exc)


def op_list(req):
    cwd = _abs(req.get("path"))
    if not os.path.isdir(cwd):
        return {"ok": False, "error": "Not a folder: " + cwd}
    dirs_only = bool(req.get("dirs_only"))
    entries = []
    try:
        it = os.scandir(cwd)
    except OSError as exc:
        return {"ok": False, "error": "Can't open " + cwd + ": " + _why(exc)}
    with it:
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
            entries.append([de.name, is_dir, size, mtime])
    return {"ok": True, "cwd": cwd, "entries": entries}


def op_mkdir(req):
    parent = _abs(req.get("parent"))
    path = os.path.join(parent, req.get("name") or "")
    try:
        os.mkdir(path)
    except OSError as exc:
        return {"ok": False, "error": "Can't create " + path + ": " + _why(exc)}
    return {"ok": True, "path": path}


def op_view(req):
    path = _abs(req.get("path"))
    max_bytes = int(req.get("max_bytes") or 1000000)
    if not os.path.isfile(path):
        return {"ok": False, "error": "Not a file: " + path}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            data = fh.read(max_bytes)
    except OSError as exc:
        return {"ok": False, "error": "Can't read " + path + ": " + _why(exc)}
    return {"ok": True, "size": size, "text": data.decode("utf-8", "replace")}


def op_rename(req):
    parent = _abs(req.get("parent"))
    old, new = req.get("old") or "", req.get("new") or ""
    src, dst = os.path.join(parent, old), os.path.join(parent, new)
    if not os.path.lexists(src):
        return {"ok": False, "error": old + " no longer exists"}
    if os.path.lexists(dst):
        return {"ok": False, "error": new + " already exists"}
    try:
        os.rename(src, dst)
    except OSError as exc:
        return {"ok": False, "error": "Can't rename " + old + ": " + _why(exc)}
    return {"ok": True, "path": dst}


def op_delete(req):
    deleted, failed = [], []
    for path in req.get("paths") or []:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                os.rmdir(path)
            else:
                os.remove(path)
            deleted.append(path)
        except OSError as exc:
            failed.append({"path": path, "error": exc.strerror or str(exc)})
    return {"ok": True, "deleted": deleted, "failed": failed}


OPS = {
    "list": op_list,
    "mkdir": op_mkdir,
    "view": op_view,
    "rename": op_rename,
    "delete": op_delete,
}


def run(req):
    fn = OPS.get(req.get("op") if isinstance(req, dict) else None)
    if fn is None:
        return {"ok": False, "error": "unknown request"}
    try:
        return fn(req)
    except Exception as exc:  # report, never a traceback the console can't read
        return {"ok": False, "error": str(exc) or exc.__class__.__name__}


def main():
    try:
        req = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except ValueError:
        req = None
    sys.stdout.write(MARKER + json.dumps(run(req)) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

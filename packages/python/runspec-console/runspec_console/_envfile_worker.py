"""_envfile_worker.py — read / write one venv's ``.runspec_env``.

Stdlib only and Python 3.8-compatible: the console sends this file's source to a
remote venv's own interpreter (``{venv}/bin/python -c <source> <op> <root>``),
escalated to the venv owner (``sudo -n -u``) when the SSH login can't read or
write the file, and calls :func:`run` in-process for a local venv. Nothing
secret is ever on the command line: a write's new text and the hash it expects
arrive as one JSON object on standard input.

Ops (the result is one JSON line after :data:`MARKER` on stdout):

* ``read`` — ``{ok, path, user, owner, exists, symlink, readable, writable,
  mode, sha, text}``. ``sha`` is ``"absent"`` when there is no file.
* ``write`` — stdin ``{"text": …, "expect": <sha>}``. Refuses (``conflict``)
  when the file changed since it was read, and refuses a symlinked file. Writes
  a temp file beside it and renames it over the old one, keeping the old mode
  (``0600`` for a new file, since the file may hold secrets). Run as root, the
  new file keeps the old file's owner (else the venv root's owner).
"""

import hashlib
import json
import os
import stat
import sys
import tempfile

MARKER = "=====RSENV "
NAME = ".runspec_env"
ABSENT = "absent"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _user_name(uid):
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except Exception:
        return ""


def _current_user():
    if hasattr(os, "geteuid"):
        name = _user_name(os.geteuid())
        if name:
            return name
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return ""


def _owner(path):
    try:
        st = os.stat(path)
    except OSError:
        return ""
    if not hasattr(os, "geteuid"):
        return ""
    return _user_name(st.st_uid)


def read(root):
    root = os.path.abspath(root)
    path = os.path.join(root, NAME)
    if not os.path.isdir(root):
        return {"ok": False, "error": "%s is not a directory." % root}
    res = {
        "ok": True,
        "path": path,
        "user": _current_user(),
        "owner": _owner(root),
        "exists": False,
        "symlink": os.path.islink(path),
        "readable": False,
        "writable": False,
        "mode": "",
        "sha": ABSENT,
        "text": "",
    }
    exists = os.path.exists(path)
    res["exists"] = exists
    dir_ok = os.access(root, os.W_OK | os.X_OK)
    if not exists:
        res["readable"] = True
        res["writable"] = dir_ok
        return res
    try:
        res["mode"] = "%04o" % stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        pass
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        res["error"] = "Can't read %s: %s" % (path, exc.strerror or exc)
        return res
    res["readable"] = True
    res["sha"] = _sha(data)
    try:
        res["text"] = data.decode("utf-8")
    except UnicodeDecodeError:
        res["error"] = "%s is not UTF-8 text; edit it by hand." % path
        return res
    res["writable"] = dir_ok and os.access(path, os.W_OK)
    return res


def write(root, payload):
    cur = read(root)
    if not cur.get("ok"):
        return cur
    path = cur["path"]
    if cur["symlink"]:
        return {
            "ok": False,
            "error": "%s is a symlink; edit its target by hand." % path,
        }
    if not cur["readable"] or cur.get("error"):
        return {"ok": False, "error": cur.get("error") or "Can't read %s." % path}
    if cur["sha"] != payload.get("expect"):
        return {
            "ok": False,
            "conflict": True,
            "error": "%s changed since it was read." % path,
        }
    if not cur["writable"]:
        return {
            "ok": False,
            "denied": True,
            "error": "%s isn't writable by %s." % (path, cur["user"]),
        }
    data = str(payload.get("text", "")).encode("utf-8")
    root = os.path.dirname(path)
    if cur["exists"]:
        st = os.stat(path)
        mode, uid, gid = stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid
    else:
        st = os.stat(root)
        mode, uid, gid = 0o600, st.st_uid, st.st_gid
    fd, tmp = tempfile.mkstemp(prefix=NAME + ".", dir=root)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(tmp, uid, gid)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {
        "ok": True,
        "path": path,
        "sha": _sha(data),
        "mode": "%04o" % mode,
        "user": cur["user"],
    }


def run(op, root, payload=None):
    try:
        if op == "read":
            return read(root)
        if op == "write":
            return write(root, payload or {})
        return {"ok": False, "error": "unknown op %r" % op}
    except Exception as exc:  # one JSON line, whatever happens
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def main(argv):
    op = argv[1] if len(argv) > 1 else ""
    root = argv[2] if len(argv) > 2 else ""
    payload = None
    if op == "write":
        try:
            payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        except ValueError:
            payload = {}
    sys.stdout.write(MARKER + json.dumps(run(op, root, payload)) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv)

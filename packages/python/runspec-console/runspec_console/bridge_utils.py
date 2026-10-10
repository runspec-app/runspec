"""bridge_utils.py — pure, Bridge-independent helper functions.

Moved out of bridge.py verbatim; imported by bridge.py and the Bridge mixins.
"""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import json
import re
import shlex
import shutil
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any


from .bridge_const import (
    PUTTY_PROXY_SESSION,
    _EVENT_LEVELS,
)


def runnable_hidden(
    name: str,
    group: str | None,
    host: str | None,
    patterns: Any,
) -> bool:
    """Return True if a runnable matches any hide *pattern* (glob).

    Each pattern is a shell glob (``fnmatch``) matched against the runnable
    ``name``. A ``group:`` / ``host:`` prefix instead matches the runnable's
    group / host, so ``group:m365`` hides a whole group and ``host:prod-*`` a
    host. Unlike the core denylist's ``_matches_any`` (empty ⇒ matches all),
    the default here is the **inverse**: an empty / missing pattern list hides
    nothing — hiding is opt-in per pattern.

    Used by :meth:`Bridge._visible_runnables` to filter the runnables cache for
    the agent toolset / Forms / catalog surfaces (see
    :func:`config.read_tool_visibility`)."""
    if not patterns:
        return False
    if isinstance(patterns, str):
        patterns = [patterns]
    for raw in patterns:
        pat = str(raw).strip()
        if not pat:
            continue
        if pat.startswith("group:"):
            target, glob = group or "", pat[len("group:") :]
        elif pat.startswith("host:"):
            target, glob = host or "", pat[len("host:") :]
        else:
            target, glob = name or "", pat
        if fnmatch.fnmatch(target, glob):
            return True
    return False


def subcommand_paths(
    runnable: dict[str, Any],
) -> list[tuple[list[str], dict[str, Any]]]:
    """Every *runnable* subcommand PATH under ``runnable``, as ``(path, node)``.

    ``path`` is the list of subcommand names to descend (``["db", "restore"]``);
    ``node`` is the cached spec node it reaches. A node is included when it does
    NOT itself require a further subcommand (leaves always qualify; an
    intermediate node qualifies only when ``requireCommand`` is false — i.e. it
    can run at its own level). Recurses to arbitrary depth, so nested verbs are
    covered. Empty when the runnable has no commands. Order is a stable pre-order
    walk of ``commands``."""
    out: list[tuple[list[str], dict[str, Any]]] = []

    def _walk(node: dict[str, Any], prefix: list[str]) -> None:
        for cn, child in (node.get("commands") or {}).items():
            if not isinstance(child, dict):
                continue
            path = prefix + [cn]
            if not bool(child.get("requireCommand")):
                out.append((path, child))
            _walk(child, path)

    _walk(runnable, [])
    return out


def _normalize_path(p: str) -> str:
    """Normalise a filesystem path to forward slashes for TOML storage.

    Browse buttons return native Windows paths with backslashes, which the
    custom TOML serialiser must escape (``\\\\``) — those escaped paths are
    valid but ugly. Forward slashes work fine for Windows file APIs and avoid
    escaping entirely. Empty / non-string inputs are passed through unchanged.
    """
    if not isinstance(p, str) or not p:
        return p
    return p.replace("\\", "/")


def _normalize_ssh_section(ssh: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an [ssh] config section with path fields normalised."""
    out = dict(ssh)
    for k in ("binary", "identityFile"):
        if isinstance(out.get(k), str):
            out[k] = _normalize_path(out[k])
    return out


def _ensure_putty_proxy_session(
    proxy: str, session: str = PUTTY_PROXY_SESSION
) -> str | None:
    """Write a PuTTY saved session carrying an HTTP proxy; return its name.

    PuTTY has no command-line proxy flag — proxy config lives in a saved session
    (Windows registry). We maintain a dedicated session holding only the proxy
    settings; `putty -load <session> -ssh -l user host` then applies the proxy
    while host/user/key still come from the command line.

    Returns the session name, or None if the proxy URL is unusable or winreg is
    unavailable (non-Windows). No proxy auth — front an auth proxy with a local
    bridge (cntlm/px) and point at http://localhost:<port>.
    """
    from urllib.parse import urlparse

    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    phost = parsed.hostname
    pport = parsed.port or 8080
    if not phost:
        return None
    try:
        import winreg  # Windows-only
    except ImportError:
        return None

    key_path = rf"Software\SimonTatham\PuTTY\Sessions\{session}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "ProxyMethod", 0, winreg.REG_DWORD, 3)  # 3 = HTTP
        winreg.SetValueEx(key, "ProxyHost", 0, winreg.REG_SZ, phost)
        winreg.SetValueEx(key, "ProxyPort", 0, winreg.REG_DWORD, pport)
        winreg.SetValueEx(key, "ProxyDNS", 0, winreg.REG_DWORD, 1)  # 1 = Auto
        winreg.SetValueEx(key, "ProxyLocalhost", 0, winreg.REG_DWORD, 0)
        winreg.SetValueEx(key, "ProxyUsername", 0, winreg.REG_SZ, "")
        winreg.SetValueEx(key, "ProxyPassword", 0, winreg.REG_SZ, "")
    return session


def _public_key_from_private(path: Path) -> str:
    """Derive the OpenSSH public-key line from an OpenSSH private key file."""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_ssh_private_key(path.read_bytes(), password=None)
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
        .strip()
    )


def _find_putty_exe(name: str = "putty.exe") -> Path | None:
    """Locate a PuTTY-suite executable (putty.exe, plink.exe, puttygen.exe, …).

    Checks common Windows install locations then falls back to PATH.
    Returns the resolved Path or None if not found.
    """

    for install_root in (
        Path(r"C:\Program Files\PuTTY"),
        Path(r"C:\Program Files (x86)\PuTTY"),
    ):
        candidate = install_root / name
        if candidate.exists():
            return candidate

    found = shutil.which(name)
    if found:
        return Path(found)
    return None


def _clean_str_list(value: Any) -> list[str]:
    """Coerce ``value`` to a list of non-empty trimmed strings (or [])."""
    if not isinstance(value, list):
        return []
    return [s for s in (str(v).strip() for v in value) if s]


def _paths(entry: dict[str, Any]) -> list[str]:
    """Normalize runspec_paths (list) or legacy runspec_path (str) to a list."""
    paths = entry.get("runspec_paths")
    if paths and isinstance(paths, list):
        return [str(p) for p in paths if p]
    rp = entry.get("runspec_path", "")
    return [str(rp)] if rp else []


# Compacted-archive filename: {runnable}.archive.{YYYYMMDD}.log[.gz]. Matched
# locally (a small copy of runspec.logs._ARCHIVE_RE) to keep the console
# decoupled from runspec internals — over/under-matching only reorders the
# newest-first read, never changes attribution (that comes from the record body).
_ARCHIVE_RE = re.compile(r"\.archive\.\d{8}\.log(?:\.gz)?$")


def _is_archive(path: Path) -> bool:
    """True for a compacted archive file (never a per-run or single-mode file)."""
    return bool(_ARCHIVE_RE.search(path.name))


def _mtime_iso(mtime: float) -> str:
    """A file mtime → UTC ISO-8601 string in the same shape as a record ``ts``.

    Records write ``ts`` as ``datetime.fromtimestamp(created, tz=utc).isoformat()``
    (see runspec logging_setup). A per-run file's mtime is its last write, i.e.
    ``>=`` the run_summary's ``created``, so ``_mtime_iso(mtime) >= summary.ts``
    lexicographically — the invariant the History newest-first early-stop relies on.
    """
    from datetime import timezone

    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _log_globs(runnable: str | None) -> list[str]:
    """Filename patterns to read for a runnable (or all of them).

    Matches the legacy single file (``{runnable}.log``), per-invocation files
    (``{runnable}.{ts}.{run_id}.log``, store="per-run"), and compacted archives
    (``{runnable}.archive.*.log.gz``). Discovery only — the runnable a record
    belongs to is read from the record body (``extra.runnable``), not the
    filename, so over-matching is harmless. ``{runnable}.log`` is listed
    separately because ``{runnable}.*.log`` does not match it.
    """
    if runnable:
        return [f"{runnable}.*.log", f"{runnable}.log", f"{runnable}.*.log.gz"]
    return ["*.log", "*.log.gz"]


def _remote_log_script(
    log_dir: str | list[str], runnable: str | None, limit: int | None = None
) -> str:
    """Shell snippet that frames and streams matching log files over SSH.

    ``log_dir`` may be a single directory or several (e.g. each venv's ``logs``
    plus ``~/logs``); all are scanned. Each file is preceded by a
    ``\\x00RUNSPEC_LOG:<basename>`` frame (the name is only a fallback —
    attribution comes from the record body). Compacted ``.gz`` archives are
    decompressed with ``gzip -dc``; plain files are ``cat``-ed.

    ``limit`` (History) streams only the newest ``limit`` files — ``ls -1t``
    orders by mtime descending across every matched glob and ``head`` caps the
    count, so a busy host's output can't be truncated by the SSH timeout (the
    root cause of History's "rows trickle in" on remotes). Omitted (analytics)
    keeps the unbounded ``for``-loop scan of every file. In both forms
    non-matching globs are skipped (``[ -f ]`` guard / ``ls`` erroring to
    ``/dev/null``), so no ``nullglob`` is required.
    """
    dirs = [log_dir] if isinstance(log_dir, str) else list(log_dir)
    patterns = " ".join(f"{d}/{g}" for d in dirs for g in _log_globs(runnable))
    # Frames carry a per-call nonce generated *on the host* and announced first
    # (``\x00RUNSPEC_NONCE:<n>``): a log file's content — which, with the logs
    # inbox, any local account can write — can't forge a frame boundary it can't
    # predict. ``[ -f ]`` also rejects a symlink's missing target; ``-L`` skips a
    # symlink outright (the inbox is world-writable). The NUL is written as the
    # octal ``\\000`` — POSIX printf; dash's printf has no ``\\x`` escapes.
    body = (
        '[ -f "$f" ] && [ ! -L "$f" ] || continue; '
        'printf "\\000RUNSPEC_LOG:%s:%s\\n" "$n" "$(basename "$f" .log)"; '
        'case "$f" in *.gz) gzip -dc "$f";; *) cat "$f";; esac; '
    )
    nonce = (
        "n=$(od -An -N8 -tx1 /dev/urandom 2>/dev/null | tr -d ' \\n'); "
        '[ -n "$n" ] || n=$$$(date +%s%N 2>/dev/null); '
        'printf "\\000RUNSPEC_NONCE:%s\\n" "$n"; '
    )
    if limit is not None:
        return (
            f"{nonce}ls -1t {patterns} 2>/dev/null | head -n {int(limit)} | "
            f"while IFS= read -r f; do {body}done 2>/dev/null"
        )
    return f"{nonce}for f in {patterns}; do {body}done 2>/dev/null"


_NONCE_HEAD = "\x00RUNSPEC_NONCE:"
_FRAME_HEAD = "\x00RUNSPEC_LOG:"


def iter_log_frames(stdout: str) -> list[tuple[str, str]]:
    """Split a :func:`_remote_log_script` stream into ``(name, text)`` per file.

    A stream that opens with a ``\\x00RUNSPEC_NONCE:<n>`` line only honours frame
    lines carrying that nonce (``\\x00RUNSPEC_LOG:<n>:<name>``) — anything else,
    including a frame-looking line inside a file's content, is content. A stream
    without the header (an older script) keeps the plain ``<name>`` frames."""
    frames: list[tuple[str, str]] = []
    name: str | None = None
    lines: list[str] = []
    nonce: str | None = None
    for i, line in enumerate(stdout.splitlines()):
        if i == 0 and line.startswith(_NONCE_HEAD):
            nonce = line[len(_NONCE_HEAD) :]
            continue
        if line.startswith(_FRAME_HEAD):
            rest = line[len(_FRAME_HEAD) :]
            if nonce is None or rest.startswith(nonce + ":"):
                if name is not None:
                    frames.append((name, "\n".join(lines)))
                name = rest if nonce is None else rest[len(nonce) + 1 :]
                lines = []
                continue
        lines.append(line)
    if name is not None:
        frames.append((name, "\n".join(lines)))
    return frames


# ── the logs inbox, read on the remote host (docs/design/venv-logs-inbox.md) ─────
# Runs by accounts other than a venv's owner wait in ``{venv}/logs/inbox`` until the
# owner's sweep indexes them (core runspec >= 0.66). This stdlib fragment — appended
# to the read / sync / running snippets below — returns them as runs-index-shaped
# rows (``pending``, ``source="inbox"``, ``os_user`` from the file's owner). It
# mirrors core's ``runspec.inbox.inbox_rows`` (the one parser), so it runs under any
# venv's python whatever runspec it has; tests/test_history_cache.py checks the two
# agree. Safe in a world-writable dir: the inbox must be a real sticky 01777 dir
# owned by the venv owner; entries are lstat-checked (regular, one link, readable)
# and opened ``O_NONBLOCK|O_NOFOLLOW`` against the dir handle, so a planted symlink
# or FIFO can't redirect or hang the read; a per-run file yields only its own run.
# Liveness is the writer's lock (a held ``flock`` = running), never the pid alone.
_INBOX_FRAGMENT = r"""
import json, os, re as _re, sqlite3, stat as _st, time as _time
_RS_NAME = _re.compile(r"\.\d{8}T\d{6}Z\.([0-9a-fA-F-]{8,})\.log$")
_RS_UUID = _re.compile(r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:-[0-9a-fA-F]{8})?$")
_RS_MAX = 64 * 1024 * 1024
def _rs_host():
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return ""
def _rs_tok(pid):
    try:
        with open("/proc/%d/stat" % int(pid), encoding="ascii", errors="replace") as fh:
            data = fh.read()
        tail = data[data.rfind(")") + 1:].split()
        return tail[19] if len(tail) > 19 else ""
    except Exception:
        return ""
def _rs_alive(pid, tok):
    # hidepid-aware: /proc can hide another account's live process, so a missing
    # start token falls back to kill(pid, 0) (EPERM = alive) instead of "dead".
    if not pid:
        return False
    if tok:
        t = _rs_tok(pid)
        if t:
            return t == tok
    if os.name == "nt":
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
def _rs_unsafe(d):
    # A world-writable or sticky dir must never hold a runs index (sidecar planting).
    # POSIX only (like core's runsdb._unsafe_dir): Windows reports every dir 0o777.
    if os.name == "nt":
        return False
    try:
        return bool(os.stat(d).st_mode & 0o1002)
    except Exception:
        return False
def _rs_ro(db, timeout=2.0):
    # Read-only open of a runs index. ``mode=ro`` on a WAL database still has to
    # create the ``-shm`` sidecar, which a non-owner can't in an owner-only (0755)
    # logs dir once the owner's last connection has removed it. With no ``-wal``
    # the main file holds every committed row, so fall back to ``immutable=1``
    # (mirrors core's runsdb._connect).
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=timeout)
    try:
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        return con
    except sqlite3.OperationalError:
        con.close()
        if os.path.exists(db + "-wal"):
            raise
    return sqlite3.connect("file:%s?mode=ro&immutable=1" % db, uri=True, timeout=timeout)
def _rs_rid(name):
    m = _RS_NAME.search(name)
    if not m:
        return None
    u = _RS_UUID.match(m.group(1))
    return u.group(1) if u else m.group(1)
def _rs_ts(v):
    import datetime as _dt
    if not isinstance(v, str) or not v:
        return None
    try:
        t = _dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)
def _rs_empty(v):
    return v is None or v == "" or v == [] or v == {}
def _rs_row(rid, recs):
    import datetime as _dt
    start, summ, sts, end, ets, ex, xts, first, hdb, seen = {}, None, None, None, None, None, None, None, True, False
    for rec in recs:
        e = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
        if e.get("run_id") != rid:
            continue
        seen = True
        if first is None:
            first = rec.get("ts")
        ev = e.get("event")
        if ev == "run_start":
            if e.get("history_db") is False:
                hdb = False
                continue
            for k, v in e.items():
                if _rs_empty(start.get(k)) and not _rs_empty(v):
                    start[k] = v
        elif ev == "run_summary":
            summ, sts = e, rec.get("ts")
        elif ev == "run_end":
            end, ets = e, rec.get("ts")
        elif ev == "exec_result":
            ex, xts = e, rec.get("ts")
    if not seen:
        return None
    src = summ or {}
    row = {"run_id": rid, "history_db": hdb}
    row["runnable"] = start.get("runnable") or src.get("runnable") or ""
    cmd = start.get("command_path") or src.get("command_path") or []
    row["command"] = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
    for k in ("user", "user_target", "agent", "trigger", "skill", "autonomy"):
        v = start.get(k)
        row[k] = v if not _rs_empty(v) else src.get(k)
    for k in ("pid", "host", "proc_start", "run_as", "label", "exec_uid", "venv"):
        row[k] = start.get(k)
    args = start.get("invocation_args") or src.get("invocation_args") or {}
    row["args"] = args if isinstance(args, dict) else {}
    if summ is not None:
        exc = summ.get("exception")
        code = summ.get("exit_code", 1 if exc else 0)
        row.update(status="failed" if (exc or code) else "completed", exit_code=code, end_ts=sts,
                   duration_ms=summ.get("duration_ms"), events=summ.get("events"), exception=exc)
    elif end is not None:
        code = end.get("exit_code", 0)
        row.update(status="failed" if code else "completed", exit_code=code, end_ts=ets, duration_ms=end.get("duration_ms"))
    else:
        row["status"] = "running"
    if ex is not None:
        oc = {"status": ex.get("status") or ("completed" if not ex.get("exit_code") else "failed"),
              "exit_code": ex.get("exit_code"), "duration_ms": ex.get("duration_ms"), "end_ts": xts}
        row["exec"] = oc
        row.update(oc)
        for k in ("label", "trigger"):
            if _rs_empty(row.get(k)) and not _rs_empty(ex.get(k)):
                row[k] = ex.get(k)
    st0 = start.get("start_ts")
    if not st0 and summ is not None:
        t = _rs_ts(sts)
        st0 = (t - _dt.timedelta(milliseconds=summ.get("duration_ms"))).isoformat() if t is not None and isinstance(summ.get("duration_ms"), int) else sts
    row["start_ts"] = st0 or first
    row["finished"] = summ is not None or end is not None or ex is not None
    return row
def _rs_inbox(d, max_run_hours=24):
    out = []
    try:
        import fcntl, pwd
    except Exception:
        return out
    logs = os.path.expanduser(d)
    box = os.path.join(logs, "inbox")
    try:
        owner = os.stat(os.path.dirname(os.path.abspath(logs))).st_uid
        ldev = os.stat(logs).st_dev
        if os.path.lexists(os.path.join(logs, ".no-inbox")):
            return out
        dfd = os.open(box, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except Exception:
        return out
    now = _time.time()
    host = _rs_host()
    try:
        s0 = os.fstat(dfd)
        if not _st.S_ISDIR(s0.st_mode) or s0.st_uid != owner or s0.st_dev != ldev or _st.S_IMODE(s0.st_mode) != 0o1777:
            return out
        for name in sorted(os.listdir(dfd)):
            rid = _rs_rid(name)
            if rid is None:
                continue
            try:
                l = os.lstat(name, dir_fd=dfd)
            except OSError:
                continue
            if not _st.S_ISREG(l.st_mode) or l.st_nlink != 1 or not l.st_mode & _st.S_IROTH or l.st_size > _RS_MAX:
                continue
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0), dir_fd=dfd)
            except OSError:
                continue
            try:
                f = os.fstat(fd)
                if (f.st_dev, f.st_ino) != (l.st_dev, l.st_ino) or not _st.S_ISREG(f.st_mode) or f.st_nlink != 1:
                    continue
                held = False
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    held = True
                except OSError:
                    pass
                data = b""
                while len(data) <= _RS_MAX:
                    c = os.read(fd, 1 << 20)
                    if not c:
                        break
                    data += c
            finally:
                os.close(fd)
            recs = []
            for ln in data[:_RS_MAX + 1].decode("utf-8", "replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    recs.append(r)
            row = _rs_row(rid, recs)
            if row is None or row.get("history_db") is False:
                continue
            row["log_file"] = name
            row["source"] = "inbox"
            try:
                row["os_user"] = pwd.getpwuid(f.st_uid).pw_name
            except Exception:
                row["os_user"] = str(f.st_uid)
            lim = f.st_ctime + 5
            for k in ("start_ts", "end_ts"):
                t = _rs_ts(row.get(k))
                if t is not None and t.timestamp() > lim:
                    import datetime as _dt
                    row[k] = _dt.datetime.fromtimestamp(f.st_ctime, tz=_dt.timezone.utc).isoformat()
            if not row["finished"]:
                if held:
                    row["status"] = "running"
                else:
                    if row.get("host") == host and row.get("pid"):
                        dead = not _rs_alive(row.get("pid"), row.get("proc_start"))
                    else:
                        t = _rs_ts(row.get("start_ts"))
                        dead = now - (t.timestamp() if t is not None else f.st_ctime) > max(1, int(max_run_hours)) * 3600
                    if dead:
                        row["status"] = "crashed"
            row["pending"] = True
            out.append(row)
    except Exception:
        pass
    finally:
        os.close(dfd)
    return out
"""


# ── DB-backed History read: a standalone stdlib program (no runspec import) ──────
# Runs as `{venv}/bin/python -c <this> <json-arg>` on the host (design §9). It reads
# each venv's runspec_runs.db in mode=ro (sees uncheckpointed WAL rows cross-process,
# per docs §12) and emits, per input dir, {has_db, rows} — finished runs only, newest
# first, windowed. Keyed by the *input* dir string (so the console maps a row back to
# its venv group), but opened via expanduser so "~/logs" resolves on the host. Raw
# rows only; the console maps them to the HistoryRecord shape via db_row_to_record,
# so the shape logic lives in one tested place, not duplicated in this snippet.
_DB_READ_SNIPPET = r"""
import json, os, sqlite3, sys
a = json.loads(sys.argv[1])
since = a.get("since")
limit = int(a.get("limit") or 500)
out = {}
for d in a.get("dirs", []):
    db = os.path.join(os.path.expanduser(d), "runspec_runs.db")
    # The inbox first, then the index (a run swept in between is seen once, not
    # zero times — the index wins the dedupe on the console side).
    e = {"has_db": os.path.exists(db), "rows": [], "pending": [r for r in _rs_inbox(d) if r["status"] != "running"]}
    if e["has_db"] and _rs_unsafe(os.path.expanduser(d)):
        e["has_db"] = False; e["error"] = "refusing a runs index in a world-writable or sticky dir"
    if e["has_db"]:
        try:
            con = _rs_ro(db)
            con.row_factory = sqlite3.Row
            q = "SELECT * FROM runs WHERE status != 'running'"
            pr = []
            if since:
                q += " AND start_ts >= ?"; pr.append(since)
            q += " ORDER BY start_ts DESC LIMIT ?"; pr.append(limit)
            e["rows"] = [{k: r[k] for k in r.keys()} for r in con.execute(q, pr).fetchall()]
            con.close()
        except Exception as ex:
            e["error"] = str(ex); e["rows"] = []
    out[d] = e
sys.stdout.write(json.dumps(out))
"""


def db_read_cmd(
    python_path: str, dirs: list[str], since: str | None, limit: int
) -> str:
    """The command to read the per-venv runs index on a remote host —
    ``{python} -c <snippet> <json-arg>`` (stdlib ``sqlite3`` only, ``mode=ro``, no
    runspec import). ``dirs`` are the venv logs dirs (+ ``~/logs``); the snippet
    expands ``~`` and keys its JSON result by the input dir. ``since`` is an ISO
    lower bound on ``start_ts``."""
    arg = json.dumps({"dirs": list(dirs), "since": since, "limit": int(limit)})
    return f"{shlex.quote(python_path)} -c {shlex.quote(_INBOX_FRAGMENT + _DB_READ_SNIPPET)} {shlex.quote(arg)}"


# ── incremental pull of the runs index (docs/design/history-incremental-pull.md) ──
# The remote half of the console's History cache: for each logs dir, given the
# console's cursor (db_id, seq), return only what changed since — the same contract
# as core's ``runspec.runsdb.changes`` (mirrored here, stdlib only, so it runs under
# any venv's python whatever runspec it has; tests/test_history_cache.py checks the
# two agree). Per dir: ``none`` (no index), ``legacy`` (a pre-v4 index — no change
# sequence; the console reads it directly), ``reset`` (no/stale cursor → a windowed
# snapshot) or ``delta``; ``more`` when a page was capped at ``limit``.
_DB_SYNC_SNIPPET = r"""
import json, os, sqlite3, sys
a = json.loads(sys.argv[1])
limit = max(1, int(a.get("limit") or 2000))
win = a.get("window_start")
out = {}
def pull(con, cur):
    con.execute("BEGIN")
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_meta'").fetchone() or \
           not any(r[1] == "seq" for r in con.execute("PRAGMA table_info(runs)").fetchall()):
            return {"mode": "legacy"}
        meta = dict(con.execute("SELECT key, value FROM sync_meta").fetchall())
        if "db_id" not in meta:
            return {"mode": "legacy"}
        db_id, top, floor = str(meta["db_id"]), int(meta.get("seq") or 0), int(meta.get("floor") or 0)
        seq = cur.get("seq")
        valid = seq is not None and cur.get("db_id") == db_id and floor <= int(seq) <= top
        after = int(seq) if valid else 0
        q, pr = "SELECT * FROM runs WHERE seq > ?", [after]
        if win:
            q += " AND start_ts >= ?"; pr.append(win)
        rows = [{k: r[k] for k in r.keys()} for r in con.execute(q + " ORDER BY seq LIMIT ?", pr + [limit + 1]).fetchall()]
        tombs = []
        if valid:
            tombs = con.execute("SELECT run_id, seq FROM runs_tombstones WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit + 1)).fetchall()
        merged = sorted([(r["seq"], r) for r in rows] + [(t[1], t[0]) for t in tombs], key=lambda x: x[0])
        more = len(merged) > limit
        page = merged[:limit]
        return {"mode": "delta" if valid else "reset", "db_id": db_id,
                "seq": page[-1][0] if more else top,
                "rows": [i for _, i in page if isinstance(i, dict)],
                "deleted": [i for _, i in page if not isinstance(i, dict)],
                "more": more}
    finally:
        con.rollback()
for d, cur in (a.get("dirs") or {}).items():
    db = os.path.join(os.path.expanduser(d), "runspec_runs.db")
    # Finished runs still waiting in the logs inbox — read *before* the index, a
    # per-pull snapshot the console dedupes against the index (index wins).
    pending = [r for r in _rs_inbox(d) if r["status"] != "running"] if a.get("inbox") else None
    if not os.path.exists(db):
        out[d] = {"mode": "none"}
    elif _rs_unsafe(os.path.expanduser(d)):
        out[d] = {"mode": "error", "error": "refusing a runs index in a world-writable or sticky dir"}
    else:
        try:
            con = _rs_ro(db)
            con.row_factory = sqlite3.Row
            try:
                out[d] = pull(con, cur or {})
            finally:
                con.close()
        except Exception as ex:
            out[d] = {"mode": "error", "error": str(ex)}
    if pending is not None:
        out[d]["pending"] = pending
sys.stdout.write(json.dumps(out))
"""


def db_sync_cmd(
    python_path: str,
    cursors: dict[str, dict[str, Any]],
    *,
    limit: int,
    window_start: str | None,
    inbox: bool = True,
) -> str:
    """The command for one incremental-pull round-trip — ``{python} -c <snippet>
    <json-arg>``. ``cursors`` maps each logs dir to ``{"db_id", "seq"}`` (empty for
    a first pull); the result is keyed by the same dirs. With ``inbox`` each dir's
    result also carries ``pending`` — its finished runs still in the logs inbox
    (only asked on a pull's first page; later pages would repeat it)."""
    arg = json.dumps(
        {
            "dirs": cursors,
            "limit": int(limit),
            "window_start": window_start,
            "inbox": bool(inbox),
        }
    )
    return f"{shlex.quote(python_path)} -c {shlex.quote(_INBOX_FRAGMENT + _DB_SYNC_SNIPPET)} {shlex.quote(arg)}"


# ── live "what's running" read: running rows + pid-liveness, on the remote host ──
# The finished-history snippet above filters `status != 'running'`; this one is its
# mirror for the live view (Phase 3 part 2b). It selects the `running` rows and, on
# the remote host that owns the pids, applies the same liveness filter
# runsdb.list_runs(live=True) does — pid alive (proc_start-guarded, /proc field 22)
# for a row whose `host` is this host, other-host rows passed through unchecked
# (mirroring runsdb._liveness_filter) — so a crashed/orphaned row never reads as
# running. Raw rows only; the console maps them via db_row_to_running (one tested
# place, as with the finished read).
_DB_RUNNING_SNIPPET = r"""
import json, os, socket, sqlite3, sys
a = json.loads(sys.argv[1])
limit = int(a.get("limit") or 200)
try:
    host = socket.gethostname()
except Exception:
    host = ""
def proc_start(pid):
    try:
        with open("/proc/%d/stat" % int(pid), encoding="ascii", errors="replace") as fh:
            data = fh.read()
        tail = data[data.rfind(")") + 1:].split()
        return tail[19] if len(tail) > 19 else ""
    except Exception:
        return ""
def win_alive(pid):
    # Windows OpenProcess probe — os.kill(pid, 0) on Windows is TerminateProcess
    # (it would KILL the process), so a Windows fleet host must never take that
    # path. Fails safe (assume alive) so a probe error is never destructive.
    try:
        import ctypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = (ctypes.c_uint, ctypes.c_int, ctypes.c_uint)
        h = k.OpenProcess(0x1000, 0, int(pid))
        if not h:
            return ctypes.get_last_error() != 87  # 87 = ERROR_INVALID_PARAMETER → dead
        k.CloseHandle.argtypes = (ctypes.c_void_p,)
        k.CloseHandle(h)
        return True
    except Exception:
        return True
def alive(pid, tok):
    if not pid:
        return False
    if tok:
        t = proc_start(pid)
        if t:
            return t == tok
        # No token: gone — or hidden by /proc hidepid (another account's live
        # run). kill(pid, 0) tells them apart (EPERM = alive).
    if os.name == "nt":
        return win_alive(pid)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
out = {}
for d in a.get("dirs", []):
    db = os.path.join(os.path.expanduser(d), "runspec_runs.db")
    # Live runs in the logs inbox (the writer holds its file's lock) — other
    # accounts' runs, which never write the index.
    e = {"has_db": os.path.exists(db), "rows": [], "pending": [r for r in _rs_inbox(d) if r["status"] == "running"]}
    if e["has_db"] and _rs_unsafe(os.path.expanduser(d)):
        e["has_db"] = False; e["error"] = "refusing a runs index in a world-writable or sticky dir"
    if e["has_db"]:
        try:
            con = _rs_ro(db)
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM runs WHERE status='running' ORDER BY start_ts DESC LIMIT ?", (limit,)).fetchall()
            con.close()
            keep = []
            for r in rows:
                rr = {k: r[k] for k in r.keys()}
                if rr.get("host") == host and not alive(rr.get("pid"), rr.get("proc_start")):
                    continue  # dead pid on this host — not really running
                keep.append(rr)
            e["rows"] = keep
        except Exception as ex:
            e["error"] = str(ex); e["rows"] = []
    out[d] = e
sys.stdout.write(json.dumps(out))
"""


def db_running_cmd(python_path: str, dirs: list[str], limit: int) -> str:
    """The command to read *live* (running) rows from the per-venv runs index on a
    remote host — ``{python} -c <snippet> <json-arg>`` (stdlib ``sqlite3``,
    ``mode=ro``, no runspec import). Unlike :func:`db_read_cmd` (finished history)
    this selects ``status='running'`` rows and applies the same pid-liveness filter
    ``runsdb.list_runs(live=True)`` does — on the remote host, which owns the pids —
    so a crashed/orphaned row never reads as running. ``dirs`` are the venv logs
    dirs (+ ``~/logs``); the snippet expands ``~`` and keys its JSON by the input
    dir."""
    arg = json.dumps({"dirs": list(dirs), "limit": int(limit)})
    return f"{shlex.quote(python_path)} -c {shlex.quote(_INBOX_FRAGMENT + _DB_RUNNING_SNIPPET)} {shlex.quote(arg)}"


# The historic SSH key comment. ``key_marker()`` returns the live one (the
# white-label package name); this default is what an unbranded install uses and
# what keys pushed before the marker followed the package name carry. Never
# change it — see bridge_config_ssh._KEY_MARKER.
# ── denylist: escalate a write to the venv owner ─────────────────────────────

DENYLIST_WRITE_ACTIONS = frozenset({"add", "all", "clear", "mode", "remove", "enable"})


def runspec_missing(code: int, out: str, err: str, runspec_path: str) -> bool:
    """True when a ``<runspec_path> …`` call failed because no ``runspec`` is
    installed at that path — the host entry names a venv that hasn't been
    created (yet). The shell reports it as exit 127 / "No such file or
    directory" naming the path; that is "not installed", not a denylist error."""
    if code == 0:
        return False
    text = f"{out or ''}\n{err or ''}"
    return code == 127 or (runspec_path in text and "No such file or directory" in text)


def runspec_missing_note(runspec_path: str) -> str:
    """The one-line row/summary text for :func:`runspec_missing`."""
    return f"no runspec at {runspec_path} — venv not created yet, or remove the path from the host entry"


DENYLIST_AS_MARKER = "=====AS "
_PERMISSION_DENIED_RE = re.compile(
    r"permission denied|not permitted|PermissionError|EACCES|EPERM|Errno 13",
    re.IGNORECASE,
)


def looks_permission_denied(text: str) -> bool:
    """True when a failed reply is a filesystem permission refusal — a
    ``runspec deny`` write (the 0.51.2+ one-line message *or* an older core's
    traceback) *or* a ``runspec logs share`` ``chmod``/``chgrp`` on a venv the
    login user doesn't own. Covers both EACCES ("Permission denied"/Errno 13, e.g.
    a file write) and **EPERM** ("Operation not permitted", what ``chmod``/``chgrp``
    print) so the "Make logs shared" repair distinguishes an unowned ``logs/`` dir
    (set "Run as" to escalate) from an old-core failure, not just an unwritable
    parent."""
    return bool(_PERMISSION_DENIED_RE.search(text or ""))


def venv_escalation_cmd(runspec_path: str, tail: list[str], probe: list[str]) -> str:
    """POSIX shell command running ``<runspec> <tail>`` **as the venv's owner** —
    the logs-inbox verbs (``logs share --inbox``, ``history sweep``) only the owner
    may run (runspec >= 0.66).

    The same ladder as :func:`denylist_escalation_cmd`, plus a first rung for the
    common case: the login user *is* the owner (no sudo). Else the owner via
    ``sudo -n -u <owner>``; else **root** via ``sudo -n`` — which the standard
    ``enable-passwordless-sudo scoped`` grant allows (it's ``(root)`` on the venv's
    ``bin/`` only) and which core's inbox verbs turn into the owner themselves by
    dropping privileges first. ``probe`` is a read-only argv on the same binary
    (so the probe needs exactly the grant the real call does). The chosen tier is
    announced as ``=====AS <user>`` (:data:`DENYLIST_AS_MARKER`); exit 77 when no
    tier is available."""
    rp = shlex.quote(runspec_path)
    args = " ".join(shlex.quote(a) for a in tail)
    pr = " ".join(shlex.quote(a) for a in probe)
    m = DENYLIST_AS_MARKER
    return (
        f'rp={rp}; r=$(dirname "$(dirname "$rp")"); '
        'o=$(stat -c %U "$r" 2>/dev/null || stat -f %Su "$r" 2>/dev/null); '
        "me=$(id -un 2>/dev/null); "
        'if [ -z "$o" ] || [ "$o" = "$me" ]; then '
        f'echo "{m}$me"; exec "$rp" {args}; '
        f'elif sudo -n -u "$o" "$rp" {pr} >/dev/null 2>&1; then '
        f'echo "{m}$o"; exec sudo -n -u "$o" "$rp" {args}; '
        f'elif sudo -n "$rp" {pr} >/dev/null 2>&1; then '
        f'echo "{m}root"; exec sudo -n "$rp" {args}; '
        'else echo "cannot escalate: the venv $r is owned by ${o:-?} and passwordless '
        "sudo (as ${o:-the owner} or root) for $rp was refused — grant it "
        "(enable-passwordless-sudo scoped), or run as ${o:-<owner>}: "
        f'$rp {args}"; exit 77; fi'
    )


def denylist_escalation_cmd(runspec_path: str, tail: list[str]) -> str:
    """POSIX shell command re-running ``runspec deny <tail>`` **escalated** on a
    remote host whose venv root the login user can't write.

    ``runspec deny`` writes ``runspec_denylist.toml`` into the venv root
    (``{sys.prefix}``); a venv created as a service account (``sudo -u svc
    python -m venv …``, e.g. the console's own **Provision venv…** with a run-as
    account) refuses
    the operator's write. Two tiers, probed with the read-only ``deny list`` on
    the *same* binary so the probe needs exactly the sudoers grant the write
    does (``enable-passwordless-sudo scoped`` grants ``(root)`` on the venv's
    ``bin/`` only, so an owner grant may be absent while root works):

    1. the venv root's **owner** (``stat -c %U``) when it isn't the login user
       and ``sudo -n -u <owner>`` is permitted — the file stays owned by the
       service account like the rest of the venv;
    2. else **root** via ``sudo -n``;
    3. else exit 77 with a one-line explanation (no password prompt: ``-n``
       fails fast on a TTY-less channel).

    The chosen tier is announced on stdout's first line as
    ``=====AS <user>`` (:data:`DENYLIST_AS_MARKER`) for the caller to strip and
    surface. ``tail`` is the argv after ``deny`` (already validated by the
    caller); everything is ``shlex``-quoted."""
    rp = shlex.quote(runspec_path)
    args = " ".join(shlex.quote(a) for a in tail)
    m = DENYLIST_AS_MARKER
    return (
        f'rp={rp}; r=$(dirname "$(dirname "$rp")"); '
        'o=$(stat -c %U "$r" 2>/dev/null || stat -f %Su "$r" 2>/dev/null); '
        "me=$(id -un 2>/dev/null); "
        'if [ -n "$o" ] && [ "$o" != "$me" ] '
        '&& sudo -n -u "$o" "$rp" deny list >/dev/null 2>&1; then '
        f'echo "{m}$o"; exec sudo -n -u "$o" "$rp" deny {args}; '
        'elif sudo -n "$rp" deny list >/dev/null 2>&1; then '
        f'echo "{m}root"; exec sudo -n "$rp" deny {args}; '
        'else echo "cannot escalate: the venv root $r is owned by ${o:-?} and '
        "passwordless sudo (as ${o:-the owner} or root) for $rp was refused — "
        "grant it, or run: sudo -u ${o:-<owner>} $rp deny "
        f'{args}" >&2; exit 77; fi'
    )


def split_denylist_as(out: str) -> tuple[str, str]:
    """Split the ``=====AS <user>`` first line off an escalated reply →
    ``(user, rest)``; ``("", out)`` when the marker is absent."""
    first, _, rest = (out or "").partition("\n")
    if first.startswith(DENYLIST_AS_MARKER):
        return first[len(DENYLIST_AS_MARKER) :].strip(), rest
    return "", out or ""


DEFAULT_KEY_MARKER = "runspec-console"

# Charset a marker must fit: it is interpolated into a single-quoted shell/awk
# program (rotation push) and appended to an authorized_keys line, so it must be
# one whitespace-free token with no shell/awk metacharacters. Dist names are.
_KEY_MARKER_RE = re.compile(r"[A-Za-z0-9._-]+")


def valid_key_marker(marker: str) -> bool:
    """True when ``marker`` is a single safe token usable as the key comment."""
    return bool(marker) and _KEY_MARKER_RE.fullmatch(marker) is not None


def key_marker() -> str:
    """The comment this install tags its generated public keys with, and the one
    its rotation prunes: the white-label package name (the same source that names
    the config folder — ``config.app_slug()``), or ``runspec-console`` for a
    default install / when the slug would not be safe inside the awk program.
    Two branded installs on one machine therefore prune only their own keys."""
    try:
        from . import config as config_mod  # lazy: config never imports us

        slug = config_mod.app_slug()
    except Exception:
        slug = ""
    return slug if valid_key_marker(slug) else DEFAULT_KEY_MARKER


def _write_ppk_v2(
    dest: Path, raw_priv: bytes, raw_pub: bytes, comment: str | None = None
) -> None:
    """Write a PuTTY PPK v2 file for an ed25519 key pair (no passphrase).

    Generates a PPK v2 file compatible with all PuTTY versions, so launch_terminal
    works even on PuTTY < 0.75 which cannot read OpenSSH new-format keys.
    """
    import base64
    import hashlib
    import hmac as _hmac
    import struct
    import textwrap

    key_type = b"ssh-ed25519"
    enc_type = b"none"
    comment = comment or key_marker()
    comment_b = comment.encode()

    # SSH wire-format public blob
    pub_blob = (
        struct.pack(">I", len(key_type))
        + key_type
        + struct.pack(">I", len(raw_pub))
        + raw_pub
    )
    # PPK private blob: seed then public key, each length-prefixed
    priv_blob = (
        struct.pack(">I", len(raw_priv))
        + raw_priv
        + struct.pack(">I", len(raw_pub))
        + raw_pub
    )

    # MAC key = SHA1("putty-private-key-file-mac-key")
    mac_key = hashlib.sha1(b"putty-private-key-file-mac-key").digest()
    mac_data = b"".join(
        struct.pack(">I", len(f)) + f
        for f in (key_type, enc_type, comment_b, pub_blob, priv_blob)
    )
    mac_hex = _hmac.new(mac_key, mac_data, hashlib.sha1).hexdigest()

    def b64lines(data: bytes) -> tuple[int, str]:
        lines = textwrap.wrap(base64.b64encode(data).decode(), 64)
        return len(lines), "\n".join(lines)

    pub_n, pub_s = b64lines(pub_blob)
    priv_n, priv_s = b64lines(priv_blob)

    dest.write_text(
        "\n".join(
            [
                "PuTTY-User-Key-File-2: ssh-ed25519",
                "Encryption: none",
                f"Comment: {comment}",
                f"Public-Lines: {pub_n}",
                pub_s,
                f"Private-Lines: {priv_n}",
                priv_s,
                f"Private-MAC: {mac_hex}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _parse_ssh(ssh: str) -> tuple[str, str, int | None]:
    """Parse 'user@host:port' → (user, host, port). Missing parts → empty string / None."""
    user = ""
    port: int | None = None
    s = ssh
    if "@" in s:
        user, s = s.split("@", 1)
    if ":" in s:
        hostname, port_str = s.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            hostname = s
    else:
        hostname = s
    return user, hostname, port


def _iso_now() -> str:
    from datetime import timezone

    return datetime.now(timezone.utc).isoformat()


def _summary_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Pull the rich run_summary fields the Analytics tab needs.

    These are dropped from the lean HistoryRecord shape (used by the History
    tab) and only attached when ``include_extra=True`` is requested.
    """
    return {
        "autonomy": extra.get("autonomy"),
        "agent": bool(extra.get("agent")),
        "trigger": extra.get("trigger", "") or "",
        "runbook": extra.get("runbook", "") or "",
        "exception": extra.get("exception"),
        "events": extra.get("events", {}),
        "command_path": extra.get("command_path", []),
        "user_target": extra.get("user_target"),
        "invocation_args": extra.get("invocation_args", {}),
    }


def _entry_extra(entry: dict[str, Any]) -> dict[str, Any]:
    """The record's ``extra`` as a dict, tolerating anything else.

    A run_summary line normally carries an ``extra`` object, but a foreign
    ``*.log`` swept up by the broad ``*.log`` glob (or a hand-edited/truncated
    line) may have no ``extra`` key, an explicit ``extra: null``, or a non-object
    ``extra``. ``entry.get("extra", {})`` returns the default only for a *missing*
    key — an explicit ``null`` comes back as ``None`` and the subsequent
    ``.get("run_id")`` raised ``AttributeError``, crashing the whole read (History
    swallows it per host, so a new/foreign host silently dropped all its rows).
    Always hand back a real dict."""
    extra = entry.get("extra")
    return extra if isinstance(extra, dict) else {}


def _record_kind(extra: dict[str, Any]) -> str:
    """Classify a run_summary record for the History "Kind" facet.

    ``runnable`` — a real runnable run (the core engine's audit line, no
    ``builtin`` flag). ``agent`` — a whole chat/trigger/schedule agent *turn*
    (``builtin`` + ``tool == "chat"``). ``tool`` — a single built-in agent
    tool call (``builtin``, any other tool). Lets the UI keep runnable runs and
    agent turns visible while filtering the noisier per-tool-call rows."""
    if not extra.get("builtin"):
        return "runnable"
    return "agent" if extra.get("tool") == "chat" else "tool"


def log_layout(paths: list[str], *, remote: bool) -> list[tuple[str, str, str]]:
    """The log dirs a host's runs can be in — one list the History, Analytics and
    detail reads share: ``(dir, venv group, kind)`` with ``kind`` ``"venv"``
    (``{venv}/logs``), ``"inbox"`` (``{venv}/logs/inbox`` — other accounts' runs
    waiting for the owner's sweep, runspec >= 0.66; listed **before** its venv dir
    so a file swept between the two globs is still streamed once) or ``"home"``
    (``~/logs``, the fallback, tagged with the primary venv's group). ``remote``
    spells the home dir ``$HOME/logs`` for the shell; locally it's expanded."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    primary = ""
    for rp in paths:
        if not rp:
            continue
        venv = PurePosixPath(rp).parent.parent if remote else Path(rp).parent.parent
        group = venv.name
        primary = primary or group
        for d, kind in (
            (str(venv / "logs" / "inbox"), "inbox"),
            (str(venv / "logs"), "venv"),
        ):
            if d not in seen:
                seen.add(d)
                out.append((d, group, kind))
    home = "$HOME/logs" if remote else str(Path.home() / "logs")
    if home not in seen:
        out.append((home, primary, "home"))
    return out


def db_row_to_record(row: dict[str, Any], group: str, host: str) -> dict[str, Any]:
    """Map a raw ``runspec_runs.db`` row → the HistoryRecord shape :func:`_parse_log`
    emits, so History reads identically whether a row came from the runs index or a
    log parse (Phase 1 mapped the DB columns 1:1 from the ``run_summary`` fields).

    The index doesn't store the heavy per-run ``logLines`` (fetched on expand via
    ``get_run_detail``, which log-parses), so they're ``[]`` — matching the slim
    shape the History tab already ships. ``group`` is the venv the DB lives in.
    ``exit_code`` is NULL for a reaped ``crashed`` row, so it maps to non-zero
    (reads as failed, never a false success). ``args`` is stored as a JSON string."""
    run_id = str(row.get("run_id") or "")
    runnable = str(row.get("runnable") or "")
    stable_id = hashlib.md5((run_id + runnable).encode()).hexdigest()[:12]
    status = str(row.get("status") or "")
    exit_code = row.get("exit_code")
    if exit_code is None:
        exit_code = 0 if status == "completed" else 1  # crashed/running → non-zero
    args = row.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except (ValueError, TypeError):
            args = {}
    if not isinstance(args, dict):
        args = {}
    command = str(row.get("command") or "")
    return {
        "id": stable_id,
        "runId": run_id,
        "runnable": runnable,
        "group": group,
        "host": host,
        "operator": row.get("user") or "",
        "runAs": row.get("user_target") or "",
        "exitCode": int(exit_code),
        "durationMs": int(row.get("duration_ms") or 0),
        # The record's ts is the run_summary (end) ts; the index stores it as
        # end_ts, falling back to start_ts for a row that lost its finalize.
        "ts": str(row.get("end_ts") or row.get("start_ts") or ""),
        "args": args,
        "argSources": {},
        # DB stores the subcommand path space-joined; split back to the list a
        # History rerun reconstructs the invocation from.
        "commandPath": command.split() if command else [],
        "logLines": [],
        "initiatedBy": "llm" if row.get("agent") else "user",
        "trigger": str(row.get("trigger") or ""),
        # Phase 1's index column is `skill` (pre-runbook-rename); it holds the
        # runbook id the run_summary carried.
        "runbook": str(row.get("skill") or row.get("runbook") or ""),
        # §8.1 log-prune provenance (runspec 0.58.0): when this run's log file was
        # pruned and by whom — "" when the log is still present (or a pre-0.58 DB
        # without the columns). Lets the History expand panel say "log pruned by X
        # on Y — record retained" instead of a bare "no log lines" for a row whose
        # log outlived its file. .get tolerates a v1 remote DB that lacks them.
        # The index's own outcome (completed/failed/crashed/skipped) — `skipped`
        # (a `runspec exec` run that didn't start: lock held / already running)
        # carries exit_code 0 but must not read as a success. And the exec label
        # (e.g. "sched:<id>" for a shared cron schedule), "" for direct runs.
        "status": status,
        "label": row.get("label") or "",
        "logPrunedAt": row.get("log_pruned_at") or "",
        "logPrunedBy": row.get("log_pruned_by") or "",
        # v6 attribution (runspec >= 0.66, the logs inbox): the kernel-attested
        # account that actually ran it (``operator`` is self-reported — the human
        # behind sudo), and whether the run is still waiting in the venv's logs
        # inbox for the owner's sweep (``pending``).
        "account": row.get("os_user") or "",
        "pending": bool(row.get("pending")),
        "logFile": str(row.get("log_file") or ""),
        # The index only holds real runnable runs (core's configure_logging/atexit
        # hooks; chat turns write run_summary via the console, not the core DB path).
        "kind": "runnable",
        # The rich fields Analytics aggregates — the same shape _summary_extra
        # gives a log-parsed record (History's slim rows drop it).
        "extra": _db_row_extra(row, args, command),
    }


def _json_col(value: Any) -> Any:
    """A JSON-text index column → its value; ``None`` for NULL / unparseable."""
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if str(value).strip() else None
    except (ValueError, TypeError):
        return None


def _db_row_extra(
    row: dict[str, Any], args: dict[str, Any], command: str
) -> dict[str, Any]:
    """``_summary_extra``'s shape from a runs-index row. ``events`` / ``exception``
    are the v5 columns (runspec >= 0.64): ``events`` is ``None`` when the row
    predates them — *unknown*, which Analytics reports — and ``{}`` for a run that
    logged nothing."""
    events = _json_col(row.get("events"))
    exc = _json_col(row.get("exception"))
    return {
        "autonomy": row.get("autonomy"),
        "agent": bool(row.get("agent")),
        "trigger": str(row.get("trigger") or ""),
        "runbook": str(row.get("skill") or row.get("runbook") or ""),
        "exception": exc
        if isinstance(exc, dict) and (exc.get("type") or exc.get("message"))
        else None,
        "events": events if isinstance(events, dict) else None,
        "command_path": command.split() if command else [],
        "user_target": row.get("user_target"),
        "invocation_args": args,
    }


def db_row_to_running(row: dict[str, Any], group: str, host: str) -> dict[str, Any]:
    """Map a raw ``runspec_runs.db`` ``running`` row → the RunningRun shape the
    History tab's live "Running now" strip renders (Phase 3 part 2b).

    Distinct from :func:`db_row_to_record` (a finished HistoryRecord): a running
    run has no exit code, end ts or duration yet, so this carries ``startTs`` +
    ``pid`` and lets the frontend tick the elapsed time from ``startTs`` — honest
    and self-updating without a re-fetch. ``id`` is the same
    ``md5(run_id+runnable)[:12]`` a finished row gets, so a run that completes
    mid-view keeps a stable identity across the running/finished lists. ``args`` is
    stored as a JSON string in the index."""
    run_id = str(row.get("run_id") or "")
    runnable = str(row.get("runnable") or "")
    stable_id = hashlib.md5((run_id + runnable).encode()).hexdigest()[:12]
    args = row.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except (ValueError, TypeError):
            args = {}
    if not isinstance(args, dict):
        args = {}
    command = str(row.get("command") or "")
    return {
        "id": stable_id,
        "runId": run_id,
        "runnable": runnable,
        "group": group,
        "host": host,
        "operator": row.get("user") or "",
        "runAs": row.get("user_target") or "",
        "startTs": str(row.get("start_ts") or ""),
        "pid": int(row.get("pid") or 0),
        "args": args,
        "commandPath": command.split() if command else [],
        "initiatedBy": "llm" if row.get("agent") else "user",
        "trigger": str(row.get("trigger") or ""),
        # Phase 1's index column is `skill` (pre-runbook-rename); it holds the
        # runbook id the run carried.
        "runbook": str(row.get("skill") or row.get("runbook") or ""),
        "autonomy": str(row.get("autonomy") or ""),
        "account": row.get("os_user") or "",
        "pending": bool(row.get("pending")),
    }


def _parse_log(
    log_file: Path, host: str, include_extra: bool = False
) -> list[dict[str, Any]]:
    try:
        if log_file.suffix == ".gz":
            with gzip.open(log_file, "rt", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        else:
            text = log_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    # log_file.stem is only a fallback runnable name — attribution comes from
    # extra.runnable in the records (see _parse_log_by_run_id). This keeps the
    # parser correct for per-invocation filenames whose stem isn't the runnable.
    return _parse_log_text(log_file.stem, text, host, include_extra=include_extra)


_PER_RUN_NAME = re.compile(r"\.\d{8}T\d{6}Z\.([0-9a-fA-F-]{8,})(?:\.log)?$")
_UUID_SUFFIXED = re.compile(
    r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:-[0-9a-fA-F]{8})?$"
)


def _file_run_id(name: str) -> str | None:
    """The run id in a per-run log file's name (with or without ``.log`` — the
    stream frames strip it); ``None`` for any other file."""
    m = _PER_RUN_NAME.search(name)
    if not m:
        return None
    u = _UUID_SUFFIXED.match(m.group(1))
    return u.group(1) if u else m.group(1)


def _parse_log_text(
    name: str, text: str, host: str, include_extra: bool = False
) -> list[dict[str, Any]]:
    """Parse a log file's text into HistoryRecord dicts.

    When run_id is present in records (runspec >=0.18) each invocation is
    isolated by its UUID — multi-user interleaving is handled cleanly.
    Older logs without run_id fall back to sequential accumulation between
    run_summary markers.

    When ``include_extra`` is set, each record gains a nested ``extra`` dict
    carrying the rich run_summary fields (autonomy, events, exception, …) the
    Analytics tab aggregates over. The default keeps the lean History shape.
    """
    entries: list[dict[str, Any]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # Only object records are runspec log lines. A foreign ``*.log`` swept up
        # by the ``*.log`` glob can hold a bare JSON scalar/array per line; those
        # aren't records and calling ``.get`` on them would crash the parser.
        if isinstance(obj, dict):
            entries.append(obj)

    # A per-run file ({runnable}.{ts}.{run_id}.log — including one from the
    # world-writable logs inbox) contributes records only for its own run, so a
    # dropped file can't inject records into another run (mirrors core's
    # runspec.inbox.file_run_id rule).
    own = _file_run_id(name)
    if own is not None:
        entries = [
            e for e in entries if _entry_extra(e).get("run_id") in (own, None, "")
        ]

    if not entries:
        return []

    # Detect run_id presence — check first record that has extra.run_id
    has_run_id = any(_entry_extra(e).get("run_id") for e in entries)
    if has_run_id:
        return _parse_log_by_run_id(name, entries, host, include_extra=include_extra)
    return _parse_log_sequential(name, entries, host, include_extra=include_extra)


def _parse_log_by_run_id(
    name: str, entries: list[dict[str, Any]], host: str, include_extra: bool = False
) -> list[dict[str, Any]]:
    """Group log entries by run_id UUID → one HistoryRecord per invocation."""
    # Preserve insertion order of run_ids so history is chronological
    groups: dict[str, dict[str, Any]] = {}  # run_id → {lines, summary, exc}
    for entry in entries:
        extra = _entry_extra(entry)
        run_id = extra.get("run_id")
        if not run_id:
            continue
        g = groups.setdefault(run_id, {"lines": [], "summary": None, "exc": None})
        if entry.get("exc_structured"):
            # Uncaught-exception record (runspec >=0.26): lift the structured
            # frames out, don't surface it as a generic CRITICAL log line.
            g["exc"] = entry["exc_structured"]
        elif extra.get("event") == "run_summary":
            g["summary"] = entry
        else:
            g["lines"].append(
                {
                    "ts": entry.get("ts", ""),
                    "level": entry.get("level", "INFO"),
                    "message": entry.get("message", ""),
                }
            )

    records: list[dict[str, Any]] = []
    for run_id, g in groups.items():
        summary_entry = g["summary"]
        if summary_entry is None:
            continue  # in-progress run — no summary yet
        extra = _entry_extra(summary_entry)
        # Attribution comes from the record, not the filename — robust to
        # per-invocation filenames; `name` (filename stem) is only a fallback.
        runnable = extra.get("runnable") or name
        ts_raw = summary_entry.get("ts", "")
        stable_id = hashlib.md5((run_id + runnable).encode()).hexdigest()[:12]
        record = {
            "id": stable_id,
            # Raw run_id (the record id is a hash of run_id+runnable) — carried
            # so History's "View full log" drill can ask `logs_view --run <id>`.
            "runId": run_id,
            "runnable": runnable,
            "group": "",
            "host": host,
            "operator": extra.get("user", ""),
            "runAs": extra.get("user_target") or "",
            "exitCode": extra.get("exit_code", 0),
            "durationMs": extra.get("duration_ms", 0),
            "ts": ts_raw,
            # The core runnable engine records args under `invocation_args`
            # (password-typed args deliberately excluded — never audited); the
            # console's own synthetic audit records use `args`. Read the engine
            # key first so a runnable run's History row carries its args (and
            # Edit-&-rerun pre-fills), falling back to the synthetic `args`.
            "args": extra.get("invocation_args") or extra.get("args", {}),
            "argSources": extra.get("arg_sources", {}),
            # Subcommand path (empty for a plain runnable) — needed so a History
            # rerun reconstructs the original subcommand invocation.
            "commandPath": list(extra.get("command_path") or []),
            "logLines": g["lines"],
            "initiatedBy": "llm" if extra.get("agent") else "user",
            # Provenance: the event source that caused the run ("schedule"/…), or
            # "" for an interactive/manual run. Drives the History/Analytics badges.
            "trigger": extra.get("trigger", "") or "",
            # The saved runbook that drove the run ("" otherwise) — History "Runbook" column.
            "runbook": extra.get("runbook", "") or "",
            # "runnable" | "agent" | "tool" — lets History keep runnable runs and
            # agent turns visible while filtering the noisier per-tool-call rows.
            "kind": _record_kind(extra),
        }
        exc = g["exc"]
        if exc:
            # Richer than the summary's extra.exception (type/message/traceback):
            # carries the structured, table-ready call frames from #102.
            record["exception"] = {
                "type": exc.get("type"),
                "message": exc.get("message"),
                "module": exc.get("module"),
                "frames": exc.get("frames", []),
            }
        if include_extra:
            record["extra"] = _summary_extra(extra)
        records.append(record)
    return records


def _parse_log_sequential(
    name: str, entries: list[dict[str, Any]], host: str, include_extra: bool = False
) -> list[dict[str, Any]]:
    """Legacy parser for logs without run_id (runspec <0.18)."""
    records: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for entry in entries:
        extra = _entry_extra(entry)
        if extra.get("event") == "run_summary":
            ts_raw = entry.get("ts", "")
            runnable = extra.get("runnable") or name
            stable_id = hashlib.md5((ts_raw + runnable).encode()).hexdigest()[:12]
            record = {
                "id": stable_id,
                # Legacy logs (runspec <0.18) have no run_id — the drill can't
                # target a single run, so "" (the frontend hides the button).
                "runId": extra.get("run_id", "") or "",
                "runnable": runnable,
                "group": "",
                "host": host,
                "operator": extra.get("user", ""),
                "runAs": extra.get("user_target") or "",
                "exitCode": extra.get("exit_code", 0),
                "durationMs": extra.get("duration_ms", 0),
                "ts": ts_raw,
                # The core runnable engine records args under `invocation_args`
                # (password-typed args deliberately excluded — never audited); the
                # console's own synthetic audit records use `args`. Read the engine
                # key first so a runnable run's History row carries its args (and
                # Edit-&-rerun pre-fills), falling back to the synthetic `args`.
                "args": extra.get("invocation_args") or extra.get("args", {}),
                "argSources": extra.get("arg_sources", {}),
                "logLines": lines,
                "initiatedBy": "llm" if extra.get("agent") else "user",
                "trigger": extra.get("trigger", "") or "",
                "runbook": extra.get("runbook", "") or "",
                "kind": _record_kind(extra),
            }
            if include_extra:
                record["extra"] = _summary_extra(extra)
            records.append(record)
            lines = []
        else:
            lines.append(
                {
                    "ts": entry.get("ts", ""),
                    "level": entry.get("level", "INFO"),
                    "message": entry.get("message", ""),
                }
            )
    return records


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile over a pre-sorted list. Empty → 0."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


def _aggregate_analytics(
    records: list[dict[str, Any]],
    since: str,
    until: str,
    partial: bool = False,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Fold parsed run_summary records into the Analytics data contract."""
    from datetime import date

    daily: dict[str, dict[str, Any]] = {}
    buckets: dict[tuple, dict[str, Any]] = {}
    exceptions: dict[tuple, dict[str, Any]] = {}
    hosts_seen: set[str] = set()
    groups_seen: set[str] = set()
    runnables_seen: set[str] = set()
    operators_seen: set[str] = set()
    runbooks_seen: set[str] = set()
    all_durations: list[float] = []
    total = success = failure = 0

    for r in records:
        day = r.get("ts", "")[:10]
        if not day:
            continue
        ok = r.get("exitCode", 0) == 0
        dur = r.get("durationMs", 0) or 0
        host = r.get("host", "")
        group = r.get("group", "") or ""
        run = r.get("runnable", "")
        operator = r.get("operator", "") or ""
        initiated = r.get("initiatedBy", "user")
        extra = r.get("extra", {}) or {}
        autonomy = extra.get("autonomy") or ""
        runbook = r.get("runbook", "") or extra.get("runbook", "") or ""

        total += 1
        success += 1 if ok else 0
        failure += 0 if ok else 1
        all_durations.append(dur)
        hosts_seen.add(host)
        if group:
            groups_seen.add(group)
        runnables_seen.add(run)
        if operator:
            operators_seen.add(operator)
        if runbook:
            runbooks_seen.add(runbook)

        d = daily.setdefault(
            day, {"total": 0, "success": 0, "failure": 0, "durations": []}
        )
        d["total"] += 1
        d["success"] += 1 if ok else 0
        d["failure"] += 0 if ok else 1
        d["durations"].append(dur)

        bkey = (day, host, group, run, operator, initiated, autonomy, runbook)
        b = buckets.get(bkey)
        if b is None:
            b = {
                "date": day,
                "host": host,
                "group": group,
                "runnable": run,
                "operator": operator,
                "initiatedBy": initiated,
                "autonomy": autonomy,
                "runbook": runbook,
                "total": 0,
                "success": 0,
                "failure": 0,
                "durationMsSum": 0,
                "events": {lvl: 0 for lvl in _EVENT_LEVELS},
            }
            buckets[bkey] = b
        b["total"] += 1
        b["success"] += 1 if ok else 0
        b["failure"] += 0 if ok else 1
        b["durationMsSum"] += dur
        for lvl, n in (extra.get("events") or {}).items():
            if lvl in b["events"]:
                b["events"][lvl] += n

        exc = extra.get("exception")
        if exc:
            msg = exc.get("message") if isinstance(exc, dict) else str(exc)
            etype = exc.get("type", "") if isinstance(exc, dict) else ""
            label = f"{etype}: {msg}" if etype else str(msg)
            ekey = (label, run, host)
            ex = exceptions.get(ekey)
            if ex is None:
                ex = {
                    "exception": label,
                    "runnable": run,
                    "host": host,
                    "count": 0,
                    "lastTs": "",
                }
                exceptions[ekey] = ex
            ex["count"] += 1
            if r.get("ts", "") > ex["lastTs"]:
                ex["lastTs"] = r.get("ts", "")

    # Zero-filled daily series across the whole window
    daily_series: list[dict[str, Any]] = []
    try:
        start = date.fromisoformat(since)
        end = date.fromisoformat(until)
    except ValueError:
        start = end = None
    if start and end and start <= end:
        cur = start
        while cur <= end:
            key = cur.isoformat()
            d = daily.get(key)
            if d:
                durs = sorted(d["durations"])
                daily_series.append(
                    {
                        "date": key,
                        "total": d["total"],
                        "success": d["success"],
                        "failure": d["failure"],
                        "p50DurationMs": round(_percentile(durs, 50)),
                        "p95DurationMs": round(_percentile(durs, 95)),
                    }
                )
            else:
                daily_series.append(
                    {
                        "date": key,
                        "total": 0,
                        "success": 0,
                        "failure": 0,
                        "p50DurationMs": 0,
                        "p95DurationMs": 0,
                    }
                )
            cur += timedelta(days=1)

    durs_sorted = sorted(all_durations)
    duration_stats = {
        "p50": round(_percentile(durs_sorted, 50)),
        "p95": round(_percentile(durs_sorted, 95)),
        "max": round(durs_sorted[-1]) if durs_sorted else 0,
        "mean": round(sum(durs_sorted) / len(durs_sorted)) if durs_sorted else 0,
    }

    exc_list = sorted(exceptions.values(), key=lambda e: e["count"], reverse=True)

    return {
        "since": since,
        "until": until,
        "totalRuns": total,
        "successCount": success,
        "failureCount": failure,
        "durationMs": duration_stats,
        "daily": daily_series,
        "buckets": list(buckets.values()),
        "exceptions": exc_list[:50],
        "dimensions": {
            "hosts": sorted(hosts_seen),
            "groups": sorted(groups_seen),
            "runnables": sorted(runnables_seen),
            "operators": sorted(operators_seen),
            "initiatedBy": ["user", "llm"],
            "runbooks": sorted(runbooks_seen),
        },
        "partial": partial,
        "errors": errors or [],
    }


def _strip_none_toml(value: Any) -> Any:
    """Recursively drop ``None`` values so ``tomli_w`` (which has no null) can
    serialise the structure. Mirrors ``config._strip_none``."""
    if isinstance(value, dict):
        return {k: _strip_none_toml(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none_toml(v) for v in value if v is not None]
    return value


def _write_schedules(path: Path, schedules: list[dict[str, Any]]) -> None:
    """Serialise the ``[[schedule]]`` entries with ``tomli_w`` so string values
    (a natural-language ``prompt`` carrying a backslash — e.g. a Windows path —
    a quote, or a newline), nested tables (``when``/``args``/``values``/
    ``fields``), and lists are all emitted as *valid* TOML.

    The previous hand-rolled serialiser wrapped every string in bare quotes
    without escaping, so a ``prompt`` containing ``C:\\Users`` wrote
    ``"C:\\Users"`` — an invalid ``\\U`` escape that made the file unparseable
    ("Invalid hex value") and blocked every subsequent schedule create."""
    import tomli_w

    rows = [
        _strip_none_toml({k: v for k, v in s.items() if k != "nextRun"})
        for s in schedules
    ]
    path.write_text(tomli_w.dumps({"schedule": rows}), encoding="utf-8")


# ── context-window indicator helpers (docs/design/console-context-indicator.md) ──
#
# The model reports one exact prompt token count per turn; these split it across
# the labelled buckets that built it (system / console-context / memory / tools /
# catalog / history) by each bucket's *character* share, so the breakdown is an
# honest estimate that always reconciles to the exact total shown on the gauge.

# Context-window sizes (tokens) by a case-insensitive model-id substring, with a
# default for anything unrecognised. A labelled estimate, like the price table —
# the gauge shows fill against this, not a billing-exact figure.
_MODEL_WINDOWS: tuple[tuple[str, int], ...] = (
    ("claude", 200_000),
    ("gpt-4.1", 1_000_000),
    ("gpt", 128_000),
    ("gemini", 1_000_000),
)
_DEFAULT_CONTEXT_WINDOW = 200_000


def model_context_window(model: str) -> int:
    """Best-effort context-window size (tokens) for a model id, matched by
    substring. Unknown models fall back to a conservative 200k."""
    m = (model or "").lower()
    for sub, win in _MODEL_WINDOWS:
        if sub in m:
            return win
    return _DEFAULT_CONTEXT_WINDOW


def estimate_context_buckets(
    parts: dict[str, int], total_tokens: int
) -> list[dict[str, Any]]:
    """Split an exact prompt token count across labelled buckets by char share.

    ``parts`` maps a bucket key to its character count. Returns one
    ``{"key", "tokens", "est": True}`` per bucket, in ``parts`` insertion order,
    whose ``tokens`` always sum to ``total_tokens`` (largest-remainder rounding)
    so the breakdown reconciles to the exact gauge total. Slices are estimates;
    the headline total they sum to is exact."""
    keys = list(parts)
    safe = {k: max(0, int(parts.get(k, 0))) for k in keys}
    total_chars = sum(safe.values())
    if total_chars <= 0 or total_tokens <= 0:
        return [{"key": k, "tokens": 0, "est": True} for k in keys]
    raw = {k: total_tokens * safe[k] / total_chars for k in keys}
    floored = {k: int(v) for k, v in raw.items()}
    remainder = total_tokens - sum(floored.values())
    # Hand the leftover tokens to the largest fractional parts so the slices sum
    # to the exact total without any single bucket drifting by more than 1.
    for k in sorted(keys, key=lambda k: raw[k] - floored[k], reverse=True)[
        : max(0, remainder)
    ]:
        floored[k] += 1
    return [{"key": k, "tokens": floored[k], "est": True} for k in keys]


# Marker appended to a tool result when it exceeds the per-call output cap. Kept
# parseable (carries the original length + cap) so a tool card can badge the cut
# without the tool-runner having to change its plain-string return contract.
# Cap on the operator's free-text decline reason — it rides into the agent's
# tool result, so a pasted wall of text can't flood the context window.
DECLINE_REASON_MAX = 2000


def clean_decline_reason(reason: Any) -> str:
    """Normalise an operator's decline reason: a stripped string, capped at
    :data:`DECLINE_REASON_MAX` chars; ``""`` when absent/blank/not text."""
    if not isinstance(reason, str):
        return ""
    reason = reason.strip()
    if len(reason) > DECLINE_REASON_MAX:
        reason = reason[:DECLINE_REASON_MAX].rstrip() + "…"
    return reason


def decline_note(reason: str) -> str:
    """The text appended to a declined tool result when the operator gave a
    reason with their Deny — so declining also redirects the agent. ``""``
    when there is no reason (the declined result reads exactly as before)."""
    if not reason:
        return ""
    return (
        f"\nThe user's reason / what to do instead: {reason}\n"
        "Follow that guidance — don't retry the declined call unchanged."
    )


_TRUNCATION_SENTINEL = "[output truncated:"


def make_truncation_suffix(orig_len: int, cap: int) -> str:
    """The marker make truncation visible — both to the model (it says the JSON
    above is incomplete) and, via ``parse_truncation``, to the tool-card badge."""
    return (
        f"\n{_TRUNCATION_SENTINEL} {int(orig_len)} chars cut to {int(cap)} — "
        "incomplete; any JSON above is cut off mid-structure]"
    )


def format_result_for_room(output: str) -> str:
    """Format a runnable's raw result string for posting into a chat room.

    A runnable's stdout is often compact JSON. Posted verbatim it reads as a
    dense one-liner in the room — the "raw json" the operator sees. The desktop
    console renders the same string richly in its ``OutputPanel``; the room web
    app has no such renderer and shows message bodies as plain (``pre-wrap``)
    text. Since the tool runs console-side, mirror the console's raw-JSON view
    here — pretty-print a JSON object/array with a 2-space indent (the same
    shape as the console's ``<pre>`` fallback, ``JSON.stringify(v, null, 2)``)
    so it lands readable in the room and the two stay in line.

    Anything that isn't a JSON object/array — plain text, an already-formatted
    string, a gate failure prefix (``✗`` / ``[exit …]``), or a truncated
    (hence unparseable) blob — is returned unchanged.
    """
    text = output.strip()
    # Only pretty-print a JSON *container*: a bare scalar (``true``, a number, a
    # quoted string) is left exactly as the runnable wrote it.
    if not text or text[0] not in "{[":
        return output
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return output
    if not isinstance(parsed, (dict, list)):
        return output
    try:
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return output


def parse_truncation(output: str) -> dict[str, int] | None:
    """Detect the truncation marker; return ``{"orig_len", "cap"}`` or ``None``.
    Lets the UI badge a cut-off tool result without the runner changing its
    string return contract."""
    idx = output.rfind(_TRUNCATION_SENTINEL)
    if idx < 0:
        return None
    m = re.search(r"\[output truncated: (\d+) chars cut to (\d+)", output[idx:])
    if not m:
        return None
    return {"orig_len": int(m.group(1)), "cap": int(m.group(2))}


def model_kw(model: str | None) -> dict[str, str]:
    """``{"model_override": model}`` only when a per-trigger / schedule / runbook
    model is set, so the common call stays ``_get_adapter(system_override=...)``."""
    model = str(model or "").strip()
    return {"model_override": model} if model else {}


# sudo's refusals when the console escalates with `sudo -n`: it never prompts,
# so "no rule for this user" and "a rule that wants a password" both come back
# as "a password is required". Neither tells an operator what to actually do.
_SUDO_REFUSALS = (
    "a password is required",
    "a terminal is required",
    "is not in the sudoers file",
    "is not allowed to execute",
    "may not run sudo",
    "not allowed to run sudo",
)


def escalation_refusal_note(
    lines: list[str], run_as: str, login_user: str, host: str, method: str = "sudo"
) -> str:
    """A plain-English explanation for a run that failed because the console
    couldn't escalate to ``run_as``, or ``""`` when the output isn't that.

    Keeps sudo's own words out of it (they suggest ``ssh -t`` or an askpass
    helper, which is never the fix here): the fix is a passwordless rule
    letting the login account run commands as ``run_as``."""
    if not run_as:
        return ""
    text = "\n".join(lines).lower()
    who = login_user or "the login account"
    if method == "su" and "authentication failure" in text:
        return (
            f"✗  Couldn't run as {run_as} on {host}: su needs {run_as}'s password, "
            'and the console can\'t type one. Use become_method = "sudo" with a '
            f"passwordless sudo rule letting {who} run commands as {run_as}."
        )
    if not any(p in text for p in _SUDO_REFUSALS):
        return ""
    if run_as == "root":
        fix = (
            f"Ask an administrator for a passwordless sudo rule letting {who} run "
            "commands as root, or run this tool from a terminal with sudo."
        )
    else:
        fix = (
            f"Ask an administrator for a passwordless sudo rule letting {who} run "
            f"commands as {run_as}, usually by joining the group that has one."
        )
    return (
        f"✗  Couldn't run as {run_as} on {host}: sudo didn't let {who} run this "
        f"venv's tools as {run_as} without a password, and the console can't "
        f"type one. {fix}"
    )


def watch_escalation(
    on_line: Any,
    on_done: Any,
    run_as: str,
    login_user: str,
    host: str,
    method: str = "sudo",
) -> tuple[Any, Any]:
    """Wrap a run's ``(on_line, on_done)`` so a failed escalated run ends with
    :func:`escalation_refusal_note` as a last stderr line. A no-op wrap when
    nothing escalates."""
    if not run_as:
        return on_line, on_done
    seen: list[str] = []

    def line(text: str, stream: str) -> None:
        if len(seen) < 200:
            seen.append(text)
        on_line(text, stream)

    def done(exit_code: int, duration_ms: int) -> None:
        if exit_code != 0:
            note = escalation_refusal_note(seen, run_as, login_user, host, method)
            if note:
                on_line(note, "stderr")
        on_done(exit_code, duration_ms)

    return line, done


def pop_debug(
    cached: dict[str, Any] | None,
    command_path: list[str],
    args: dict[str, Any],
) -> dict[str, str]:
    """Take the console-level ``debug`` switch out of ``args`` and return the env
    that turns core's auto-added ``--debug`` on (``{}`` when off).

    Only when the runnable doesn't declare its own ``debug`` arg: one it declares
    is a real argument and stays in ``args`` untouched. An unknown spec (no
    discovery record) is also left alone, so that call behaves exactly as before.
    Shared by the Forms run (``invoke_runnable``) and the agent
    (``_run_tool_sync``); see ``executor.DEBUG_ARG`` for why it's env, not argv."""
    from . import executor

    if not cached or executor.DEBUG_ARG not in args:
        return {}
    node: dict[str, Any] = cached
    declared = {a.get("name") for a in node.get("args") or [] if isinstance(a, dict)}
    for seg in command_path:
        node = (node.get("commands") or {}).get(seg) or {}
        declared |= {
            a.get("name") for a in node.get("args") or [] if isinstance(a, dict)
        }
    if executor.DEBUG_ARG in declared:
        return {}
    value = args.pop(executor.DEBUG_ARG)
    if isinstance(value, str):
        value = value.strip().lower() in ("1", "true", "yes", "on")
    if not value:
        return {}
    return executor.debug_env(str(cached.get("name") or ""))

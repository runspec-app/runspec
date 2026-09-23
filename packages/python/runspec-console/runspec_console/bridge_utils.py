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
from pathlib import Path
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
    body = (
        '[ -f "$f" ] || continue; '
        'printf "\\x00RUNSPEC_LOG:%s\\n" "$(basename "$f" .log)"; '
        'case "$f" in *.gz) gzip -dc "$f";; *) cat "$f";; esac; '
    )
    if limit is not None:
        return (
            f"ls -1t {patterns} 2>/dev/null | head -n {int(limit)} | "
            f"while IFS= read -r f; do {body}done 2>/dev/null"
        )
    return f"for f in {patterns}; do {body}done 2>/dev/null"


# The historic SSH key comment. ``key_marker()`` returns the live one (the
# white-label package name); this default is what an unbranded install uses and
# what keys pushed before the marker followed the package name carry. Never
# change it — see bridge_config_ssh._KEY_MARKER.
# ── denylist: escalate a write to the venv owner ─────────────────────────────

DENYLIST_WRITE_ACTIONS = frozenset({"add", "all", "clear", "mode"})
DENYLIST_AS_MARKER = "=====AS "
_PERMISSION_DENIED_RE = re.compile(
    r"permission denied|PermissionError|EACCES|EPERM|Errno 13", re.IGNORECASE
)


def looks_permission_denied(text: str) -> bool:
    """True when a failed ``runspec deny`` reply is a filesystem permission
    refusal (the 0.51.2+ one-line message *or* an older core's traceback)."""
    return bool(_PERMISSION_DENIED_RE.search(text or ""))


def denylist_escalation_cmd(runspec_path: str, tail: list[str]) -> str:
    """POSIX shell command re-running ``runspec deny <tail>`` **escalated** on a
    remote host whose venv root the login user can't write.

    ``runspec deny`` writes ``runspec_denylist.toml`` into the venv root
    (``{sys.prefix}``); a venv created as a service account (``sudo -u svc
    python -m venv …``, e.g. runspec-linux's ``create-venv --run-as``) refuses
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

"""bridge_utils.py — pure, Bridge-independent helper functions.

Moved out of bridge.py verbatim; imported by bridge.py and the Bridge mixins.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


from .bridge_const import (
    PUTTY_PROXY_SESSION,
    _EVENT_LEVELS,
)


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


def _remote_log_script(log_dir: str | list[str], runnable: str | None) -> str:
    """Shell snippet that frames and streams every matching log file over SSH.

    ``log_dir`` may be a single directory or several (e.g. each venv's ``logs``
    plus ``~/logs``); all are scanned. Each file is preceded by a
    ``\\x00RUNSPEC_LOG:<basename>`` frame (the name is only a fallback —
    attribution comes from the record body). Compacted ``.gz`` archives are
    decompressed with ``gzip -dc``; plain files are ``cat``-ed. Non-matching
    globs expand to themselves and are skipped by the ``[ -f ]`` guard, so no
    ``nullglob`` is required.
    """
    dirs = [log_dir] if isinstance(log_dir, str) else list(log_dir)
    patterns = " ".join(f"{d}/{g}" for d in dirs for g in _log_globs(runnable))
    return (
        f"for f in {patterns}; do "
        f'[ -f "$f" ] || continue; '
        f'printf "\\x00RUNSPEC_LOG:%s\\n" "$(basename "$f" .log)"; '
        f'case "$f" in *.gz) gzip -dc "$f";; *) cat "$f";; esac; '
        f"done 2>/dev/null"
    )


def _write_ppk_v2(
    dest: Path, raw_priv: bytes, raw_pub: bytes, comment: str = "runspec-console"
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
        "skill": extra.get("skill", "") or "",
        "exception": extra.get("exception"),
        "events": extra.get("events", {}),
        "command_path": extra.get("command_path", []),
        "user_target": extra.get("user_target"),
        "invocation_args": extra.get("invocation_args", {}),
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
            entries.append(json.loads(raw))
        except json.JSONDecodeError:
            continue

    if not entries:
        return []

    # Detect run_id presence — check first record that has extra.run_id
    has_run_id = any(e.get("extra", {}).get("run_id") for e in entries)
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
        extra = entry.get("extra", {})
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
        extra = summary_entry.get("extra", {})
        # Attribution comes from the record, not the filename — robust to
        # per-invocation filenames; `name` (filename stem) is only a fallback.
        runnable = extra.get("runnable") or name
        ts_raw = summary_entry.get("ts", "")
        stable_id = hashlib.md5((run_id + runnable).encode()).hexdigest()[:12]
        record = {
            "id": stable_id,
            "runnable": runnable,
            "group": "",
            "host": host,
            "operator": extra.get("user", ""),
            "runAs": extra.get("user_target") or "",
            "exitCode": extra.get("exit_code", 0),
            "durationMs": extra.get("duration_ms", 0),
            "ts": ts_raw,
            "args": extra.get("args", {}),
            "argSources": extra.get("arg_sources", {}),
            "logLines": g["lines"],
            "initiatedBy": "llm" if extra.get("agent") else "user",
            # Provenance: the event source that caused the run ("schedule"/…), or
            # "" for an interactive/manual run. Drives the History/Analytics badges.
            "trigger": extra.get("trigger", "") or "",
            # The saved skill that drove the run ("" otherwise) — History "Skill" column.
            "skill": extra.get("skill", "") or "",
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
        extra = entry.get("extra", {})
        if extra.get("event") == "run_summary":
            ts_raw = entry.get("ts", "")
            runnable = extra.get("runnable") or name
            stable_id = hashlib.md5((ts_raw + runnable).encode()).hexdigest()[:12]
            record = {
                "id": stable_id,
                "runnable": runnable,
                "group": "",
                "host": host,
                "operator": extra.get("user", ""),
                "runAs": extra.get("user_target") or "",
                "exitCode": extra.get("exit_code", 0),
                "durationMs": extra.get("duration_ms", 0),
                "ts": ts_raw,
                "args": extra.get("args", {}),
                "argSources": extra.get("arg_sources", {}),
                "logLines": lines,
                "initiatedBy": "llm" if extra.get("agent") else "user",
                "trigger": extra.get("trigger", "") or "",
                "skill": extra.get("skill", "") or "",
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
    skills_seen: set[str] = set()
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
        skill = r.get("skill", "") or extra.get("skill", "") or ""

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
        if skill:
            skills_seen.add(skill)

        d = daily.setdefault(
            day, {"total": 0, "success": 0, "failure": 0, "durations": []}
        )
        d["total"] += 1
        d["success"] += 1 if ok else 0
        d["failure"] += 0 if ok else 1
        d["durations"].append(dur)

        bkey = (day, host, group, run, operator, initiated, autonomy, skill)
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
                "skill": skill,
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
            "skills": sorted(skills_seen),
        },
        "partial": partial,
        "errors": errors or [],
    }


def _toml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


def _write_schedules(path: Path, schedules: list[dict[str, Any]]) -> None:
    lines = []
    for s in schedules:
        lines.append("[[schedule]]")
        for k, v in s.items():
            if k == "nextRun":  # display-only, computed by get_schedules
                continue
            if isinstance(v, dict):
                inner = ", ".join(f"{ik} = {_toml_scalar(iv)}" for ik, iv in v.items())
                lines.append(f"{k} = {{ {inner} }}")
            else:
                lines.append(f"{k} = {_toml_scalar(v)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")

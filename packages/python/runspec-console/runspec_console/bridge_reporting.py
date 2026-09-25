"""bridge_reporting.py — ReportingMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import json
import os
import shlex
import threading
from datetime import datetime, timedelta
from pathlib import Path, PurePath, PurePosixPath
from typing import Any

from runspec import logs as core_logs

from . import bridge_utils as utils_mod
from . import hosts as hosts_mod

from .bridge_const import (
    logger,
)
from .bridge_utils import (
    _log_globs,
    _remote_log_script,
    _parse_log,
    _parse_log_text,
    _aggregate_analytics,
    _mtime_iso,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401

# Sentinel "group" id for the shared home fallback dir (~/logs). It's not a venv,
# so it can't collide with a venv name (which is a directory basename). The Logs
# tab shows it as its own entry; prune/compact/view route it to `--scope home`.
_HOME_SCOPE = "~/logs"


class ReportingMixin:
    # Built-in agent-tool calls (repo_*, outlook_*, manage_denylist, action and
    # memory tools) leave no run_summary the way a runnable does, so they used to
    # be absent from History/Analytics. We write one run_summary-shaped JSON line
    # per call to ~/logs/runspec-console-tools.log — a "local"-host log dir the
    # history scanner already reads (*.log glob) — so they surface with no parser
    # change. Attribution comes from the record body (extra.runnable = the tool
    # label), not the filename.
    _TOOL_AUDIT_FILE = "runspec-console-tools.log"
    # Every tools-audit file shares this stem (the active log plus its ``.1.log``
    # rollover). History reads only real runnable runs, and this log holds none
    # (every line here is a built-in tool call / agent turn), so History skips
    # the whole stem — see _history_files. Only Analytics reads it.
    _TOOL_AUDIT_STEM = "runspec-console-tools"
    # Cap the tools audit log so it can't grow without bound (it used to only
    # grow, and — because the parse cache keys on (mtime, size) — every appended
    # line invalidated it). At the cap the active file rolls to ``<stem>.1.log``
    # (a single backup, overwritten each roll), bounding the audit to ~2×cap.
    # ``.1.log`` keeps the ``*.log`` suffix Analytics globs, and shares the stem
    # History skips.
    _TOOL_AUDIT_MAX_BYTES = 5 * 1024 * 1024

    def _append_tool_audit(self, record: dict[str, Any]) -> None:
        """Append one JSON line to the tools audit log, rolling the file over at
        ``_TOOL_AUDIT_MAX_BYTES``. Shared by the built-in-tool / chat-turn /
        disclaimer writers. Best-effort: a logging failure must never break the
        caller (a tool result, a turn, or an acknowledgement)."""
        try:
            log_dir = Path.home() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / self._TOOL_AUDIT_FILE
            line = json.dumps(record, default=str) + "\n"
            with self._audit_lock:
                try:
                    if (
                        path.exists()
                        and path.stat().st_size > self._TOOL_AUDIT_MAX_BYTES
                    ):
                        path.replace(path.with_name(f"{self._TOOL_AUDIT_STEM}.1.log"))
                except OSError:
                    pass  # a rollover failure must not drop the append
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
        except Exception:  # noqa: BLE001 — auditing is best-effort
            logger.warning("could not write tools audit line", exc_info=True)

    def _audit_builtin_tool(
        self,
        run_id: str,
        tool_name: str,
        label: str,
        args: dict[str, Any],
        outcome: str,
        duration_ms: int,
        chat_id: str = "",
    ) -> None:
        """Append a History/Analytics audit line for one built-in agent tool
        call. ``outcome`` is "ok" | "error" | "cancelled" (→ exit_code 0/1/130).
        Best-effort: a logging failure must never break the tool result."""
        exit_code = {"ok": 0, "error": 1, "cancelled": 130}.get(outcome, 0)
        # Carry the turn's provenance (set for trigger / runbook turns) so a
        # built-in tool run is attributed like a runnable run is.
        with self._lock:
            origin = dict(self._run_origin.get(chat_id, {})) if chat_id else {}
        extra = {
            "event": "run_summary",
            "run_id": run_id,
            "runnable": label,
            "tool": tool_name,
            "user": self._current_user(),
            "exit_code": exit_code,
            "duration_ms": int(duration_ms),
            "agent": bool(origin.get("agent", True)),
            "trigger": origin.get("trigger", "") or "",
            "runbook": origin.get("runbook", "") or "",
            "cancelled": outcome == "cancelled",
            "builtin": True,
            "args": args,
        }
        record = {
            "ts": utils_mod._iso_now(),
            "level": "INFO" if exit_code == 0 else "WARNING",
            "message": f"{label} ({outcome})",
            "extra": extra,
        }
        self._append_tool_audit(record)

    def _audit_chat_turn(
        self,
        run_id: str,
        chat_id: str,
        model: str,
        duration_ms: int,
        usage: dict[str, int] | None,
        stopped: bool,
        error: bool = False,
    ) -> None:
        """Append a History/Analytics run_summary line for one completed agent
        turn — chat, trigger and schedule turns alike (the ``trigger`` field
        carries the provenance). ``exit_code`` is 130 when the operator stopped
        the turn, 1 when it failed outright (``error``), else 0. Best-effort — a
        logging failure must never break the turn."""
        exit_code = 130 if stopped else (1 if error else 0)
        with self._lock:
            origin = dict(self._run_origin.get(chat_id, {})) if chat_id else {}
        usage = usage or {}
        extra = {
            "event": "run_summary",
            "run_id": run_id,
            "runnable": "chat",
            "tool": "chat",
            "user": self._current_user(),
            "exit_code": exit_code,
            "duration_ms": int(duration_ms),
            "agent": bool(origin.get("agent", True)),
            "trigger": origin.get("trigger", "") or "",
            "runbook": origin.get("runbook", "") or "",
            "cancelled": bool(stopped),
            "builtin": True,
            "model": model,
            "input_tokens": int(usage.get("input", 0) or 0),
            "output_tokens": int(usage.get("output", 0) or 0),
            "args": {},
        }
        record = {
            "ts": utils_mod._iso_now(),
            "level": "INFO" if exit_code == 0 else "WARNING",
            "message": (
                f"chat turn ({'stopped' if stopped else ('error' if error else 'ok')})"
            ),
            "extra": extra,
        }
        self._append_tool_audit(record)

    def _audit_disclaimer_ack(
        self, *, hash_: str, version: str, user: str, ts: str
    ) -> None:
        """Append an audit line recording a disclaimer acknowledgement — the
        always-there local record (every platform), alongside the Windows event-log
        entry. Written to the same tools audit log the History scanner reads, so it
        shows up as an event without a parser change. Best-effort: a logging failure
        must never break acknowledgement."""
        record = {
            "ts": ts or utils_mod._iso_now(),
            "level": "INFO",
            "message": f"disclaimer acknowledged by {user}",
            "extra": {
                "event": "disclaimer_ack",
                "user": user,
                "disclaimer_version": version or "",
                "disclaimer_hash": hash_,
            },
        }
        self._append_tool_audit(record)

    # Cap on rows the History tab shows. Applied newest-first, and used as the
    # early-stop target so we never read more files than needed to fill it.
    _HISTORY_CAP = 200

    @staticmethod
    def _since_date(since_days: int | None) -> str | None:
        """UTC date (``YYYY-MM-DD``) ``since_days`` days back inclusive of today,
        or ``None`` for no window (``since_days`` falsy / <= 0). Matches the
        Analytics window semantics so History and Analytics agree on "last 7d"."""
        if not since_days or since_days <= 0:
            return None
        from datetime import timezone

        d = datetime.now(timezone.utc) - timedelta(days=max(0, since_days - 1))
        return d.date().isoformat()

    def get_history(
        self, host: str, runnable: str | None = None, since_days: int | None = None
    ) -> list[dict[str, Any]]:
        """Per-host run history, newest first. ``since_days`` bounds the read to
        the last N days (``None`` = no window — the default kept for the
        Forms/Logs callers); ``get_history_all`` passes the History tab's window
        so a first visit reads only a week of logs."""
        entry = self._host_entry(host)
        if entry is None:
            return []
        since_date = self._since_date(since_days)
        if entry.get("ssh"):
            return self._get_remote_history(entry, runnable, since_date)
        paths = utils_mod._paths(entry)
        globs = _log_globs(runnable)
        # Scan every venv's logs dir (tagged with its venv group) AND ~/logs. A
        # runnable writes to ~/logs when its venv dir isn't writable (managed
        # machines) — but the venv dir is still *created* on the way to that
        # fallback, so a "venv dir, else ~/logs" check reads an empty venv dir
        # and misses the real records in ~/logs. The Logs tab works because the
        # CLI's log_dirs() reads both. ~/logs records inherit the primary venv's
        # group (correct in the common single-venv case) — same as analytics.
        dir_groups: list[tuple[Path, str]] = [
            (Path(rp).parent.parent / "logs", hosts_mod.venv_name(rp))
            for rp in paths
            if rp
        ]
        fallback_group = hosts_mod.venv_name(paths[0]) if paths and paths[0] else ""
        dir_groups.append((Path.home() / "logs", fallback_group))

        files = self._history_files(dir_groups, globs)
        live = {str(p) for p, *_ in files}  # prune against the full set, not the window
        if since_date:
            # Drop files whose newest content predates the window. A per-run /
            # single / archive file's mtime is >= its newest run's ts, so an
            # mtime older than the cutoff can't hold an in-window run.
            files = [f for f in files if _mtime_iso(f[1])[:10] >= since_date]
        records = self._read_newest(files, host)
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        self._prune_history_cache(live)
        if since_date:
            records = [r for r in records if r.get("ts", "")[:10] >= since_date]
        return records[: self._HISTORY_CAP]

    # ── bounded newest-first history read (local) ──────────────────────────────

    def _history_files(
        self, dir_groups: list[tuple[Path, str]], globs: list[str]
    ) -> list[tuple[Path, float, int, bool, str]]:
        """(path, mtime, size, is_archive, group) for every matched log file.

        One ``stat()`` per file, captured here and reused as the parse-cache key
        and the mtime-vs-ts early-stop cutoff. Dirs are de-duplicated (a venv's
        logs dir and ~/logs can coincide); files by path.
        """
        out: list[tuple[Path, float, int, bool, str]] = []
        seen_dirs: set[Path] = set()
        seen_files: set[Path] = set()
        for log_dir, group in dir_groups:
            if log_dir in seen_dirs or not log_dir.exists():
                continue
            seen_dirs.add(log_dir)
            for pat in globs:
                for f in log_dir.glob(pat):
                    if f in seen_files:
                        continue
                    # The synthetic tools audit log (and its ``.1.log`` rollover)
                    # holds only built-in tool calls / agent turns — never a
                    # runnable run — so History skips it. This also spares the
                    # biggest, most-churned local file from being parsed (and
                    # from thrashing the (mtime,size) parse cache) on every read;
                    # Analytics reads it through its own path. Only bites the
                    # broad ``*.log`` sweep — a runnable-scoped glob never matches.
                    if f.name.startswith(self._TOOL_AUDIT_STEM):
                        continue
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    seen_files.add(f)
                    out.append(
                        (f, st.st_mtime, st.st_size, utils_mod._is_archive(f), group)
                    )
        return out

    def _read_newest(
        self, files: list[tuple[Path, float, int, bool, str]], host: str
    ) -> list[dict[str, Any]]:
        """Read only the newest files needed to fill _HISTORY_CAP run rows.

        Per-run files (store="per-run") are one-per-invocation whose run_summary
        — the row anchor — is the file's final line, so ``summary.ts <= file
        mtime``. Reading files newest-first, once we hold CAP rows any *unread*
        file with an mtime older than the current CAP-th row's ts cannot contain
        a newer run and is safely skipped. That invariant is why we can't just
        "stop after CAP records": a compacted archive can have a recent *mtime*
        but old *contents*, so we process fresh per-run/single files before
        archives and gate the stop on the ts cutoff, not a raw count.
        """
        fresh = sorted((f for f in files if not f[3]), key=lambda t: t[1], reverse=True)
        archives = sorted((f for f in files if f[3]), key=lambda t: t[1], reverse=True)
        records: list[dict[str, Any]] = []

        def consume(batch: list[tuple[Path, float, int, bool, str]]) -> None:
            for path, mtime, size, _is_archive, group in batch:
                if len(records) >= self._HISTORY_CAP:
                    # Newest ts we've committed to the top CAP so far.
                    cutoff = sorted((r.get("ts", "") for r in records), reverse=True)[
                        : self._HISTORY_CAP
                    ][-1]
                    if _mtime_iso(mtime) < cutoff:
                        break
                records.extend(self._parse_log_cached(path, host, mtime, size, group))

        consume(fresh)
        if len(records) < self._HISTORY_CAP:
            consume(archives)
        return records

    def _parse_log_cached(
        self, path: Path, host: str, mtime: float, size: int, group: str
    ) -> list[dict[str, Any]]:
        """Parse one log file, reusing a cached parse when (mtime, size) match.

        The parse itself runs outside the lock so concurrent History reads don't
        serialise on it; a recompute-and-store race is harmless (same input →
        same records). Records are group-tagged (the file's venv) so History's
        "View full log" drill can resolve the right venv.
        """
        key = str(path)
        with self._history_cache_lock:
            cached = self._history_parse_cache.get(key)
            if cached is not None and cached[0] == mtime and cached[1] == size:
                return cached[2]
        records = _parse_log(path, host)
        for rec in records:
            rec["group"] = group
        with self._history_cache_lock:
            self._history_parse_cache[key] = (mtime, size, records)
        return records

    def _prune_history_cache(self, live_paths: set[str]) -> None:
        """Drop cache entries for files no longer on disk (prune/compact)."""
        with self._history_cache_lock:
            stale = [k for k in self._history_parse_cache if k not in live_paths]
            for k in stale:
                del self._history_parse_cache[k]

    # Default History window: a first visit reads only the last week of logs so
    # the tab loads fast; the tab's range selector overrides it (0 / None = all).
    _HISTORY_DEFAULT_DAYS = 7

    @staticmethod
    def _slim_history_row(r: dict[str, Any]) -> dict[str, Any]:
        """A History row without the heavy per-run fields (``logLines``, the
        structured ``exception`` frames, analytics-only ``extra``). Every column
        and searched field is kept; the expanded row fetches the log on demand
        via :meth:`get_run_detail`, so the fleet-wide bulk payload stays small.

        Returns a **copy** — never mutates the parse-cached record (Forms/Logs
        read the same cached dicts through ``get_history`` and still need them
        whole)."""
        out = {
            k: v for k, v in r.items() if k not in ("logLines", "exception", "extra")
        }
        out["logLines"] = []  # keep the shape (the frontend types logLines required)
        return out

    def get_history_all(
        self, since_days: int | None = _HISTORY_DEFAULT_DAYS
    ) -> list[dict[str, Any]]:
        """History across every host — local plus *connected* remotes — newest
        first. Disconnected remotes are skipped so a slow/offline host can't hang
        the read (the History tab uses this so a run shows up regardless of which
        host it ran on; per-host scoping stays for Forms/Logs/etc.).

        ``since_days`` bounds every host's read to the last N days (default 7 —
        the tab's range selector passes 30/90/0, where 0/None means "all") so a
        first visit is a week of logs, not the whole audit trail.

        Only real **runnable runs** are returned: built-in tool calls and agent
        turns live in the tools audit log, which the read already skips — the
        ``kind`` filter here is a belt-and-suspenders guarantee. The heavy per-run
        fields are dropped (see :meth:`_slim_history_row`).

        The connected remotes are read **concurrently** (bounded by ``[refresh]
        workers``, the same cap every fleet sweep uses), so the History tab doesn't
        wait on a serial chain of SSH reads on a large fleet — one slow host adds
        its own latency, not the sum of all of them. Each host's read is isolated
        so one failure never sinks the rest."""
        with self._lock:
            remotes = list(self._hosts)
            connected = dict(self._connected_cache)
        names = ["local"]
        names += [
            h["name"] for h in remotes if h.get("name") and connected.get(h["name"])
        ]

        def _read_one(name: str) -> list[dict[str, Any]]:
            try:
                return self.get_history(name, since_days=since_days)
            except Exception:
                logger.exception("history: read failed for host %s", name)
                return []

        records: list[dict[str, Any]] = []
        if len(names) <= 1:
            records = _read_one(names[0]) if names else []
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(
                max_workers=min(self._fanout_workers(), len(names))
            ) as ex:
                for chunk in ex.map(_read_one, names):
                    records.extend(chunk)
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        records = [r for r in records if r.get("kind", "runnable") == "runnable"]
        return [self._slim_history_row(r) for r in records[:300]]

    def history_targets(self) -> list[str]:
        """Ordered hosts the History tab queries one-by-one: ``local`` first,
        then every *connected* remote (disconnected remotes are skipped, same
        rule as :meth:`get_history_all`).

        The tab fetches these with per-host :meth:`get_history_slim` calls so it
        can render each host's runs the moment they arrive and show a per-host
        timing row — one slow/offline host no longer blocks the whole view (the
        old ``get_history_all`` joined on the slowest host before returning
        anything)."""
        with self._lock:
            remotes = list(self._hosts)
            connected = dict(self._connected_cache)
        names = ["local"]
        for h in remotes:
            name = h.get("name")
            if name and name != "local" and connected.get(name):
                names.append(name)
        return names

    def get_history_slim(
        self, host: str, since_days: int | None = _HISTORY_DEFAULT_DAYS
    ) -> list[dict[str, Any]]:
        """One host's slim, runnable-only history rows — the per-host unit the
        History tab fetches (via :meth:`history_targets`) so it can render each
        host incrementally instead of waiting on the whole fleet.

        Same row shape and filtering as :meth:`get_history_all` (runnable runs
        only, heavy per-run fields dropped — see :meth:`_slim_history_row`), just
        scoped to one host. Reuses :meth:`get_history`, so the full parse-cached
        rows still back :meth:`get_run_detail` (row-expand stays a cache hit —
        the slim shape is only what crosses to the frontend)."""
        rows = self.get_history(host, since_days=since_days)
        return [
            self._slim_history_row(r)
            for r in rows
            if r.get("kind", "runnable") == "runnable"
        ]

    def get_run_detail(self, host: str, run_id: str) -> dict[str, Any]:
        """Heavy per-run detail (the recorded ``logLines``) for one run, fetched
        on demand when a History row is expanded — ``get_history_all`` ships slim
        rows, so this fills in the log for just the one run the operator opened.

        Cheap: the local read is served from the warm (mtime,size) parse cache,
        and a connected remote from the short-TTL remote-history cache the bulk
        read just populated — so expanding a row is not a fresh SSH round-trip in
        the common case. Empty when the run isn't found (aged out of the window /
        pruned) or ``run_id`` is blank (a legacy no-run_id log)."""
        if not run_id:
            return {"logLines": []}
        try:
            for r in self.get_history(host):  # no window — find the run regardless
                if r.get("runId") == run_id:
                    return {"logLines": r.get("logLines", [])}
        except Exception:
            logger.exception("history: run-detail read failed for %s/%s", host, run_id)
        return {"logLines": []}

    def _remote_log_dirs(
        self, entry: dict[str, Any], rp: str, timeout: int = 30
    ) -> list[str]:
        """Log directories for one remote venv — **path-derived, no remote runspec**.

        Returns ``[{venv}/logs (path-derived from rp), $HOME/logs]``. This used to
        run ``runspec logs status --json`` per venv (a full remote Python
        cold-start) to report ``log_dirs()`` exactly — right for pipx/~/.local
        installs whose ``sys.prefix`` isn't ``{rp}/../..``, but those N×M
        cold-starts dominated History/Analytics first-open latency. Path-derivation
        is correct for the standard venv layout (``{venv}/bin/runspec`` →
        ``{venv}/logs``), and ``$HOME/logs`` still catches fallback logs; only a
        non-standard install's *own* dir can be missed. Cached per venv path
        (``timeout`` is unused now, kept for signature compatibility)."""
        cached = self._remote_logdir_cache.get(rp)
        if cached is not None:
            return cached
        dirs = [str(PurePosixPath(rp).parent.parent / "logs"), "$HOME/logs"]
        self._remote_logdir_cache[rp] = dirs
        return dirs

    # A connected remote's stream+parse is reused within this window so the
    # History tab's 25 s poll (and its run_end refetch) doesn't re-SSH every host
    # every time. Busted on run completion (_invalidate_remote_history) so a
    # just-finished run still shows promptly; ``_HISTORY_CAP``-bounded, so the
    # cache holds at most a few hundred small records per host.
    _REMOTE_HISTORY_TTL = 15.0

    def _invalidate_remote_history(self, host: str | None = None) -> None:
        """Drop the cached remote-history read for ``host`` (or all hosts) so the
        next read re-streams — called when a run completes so History is fresh."""
        cache = getattr(self, "_remote_history_cache", None)
        if cache is None:
            return
        with self._remote_history_lock:
            if host is None:
                cache.clear()
            else:
                for k in [k for k in cache if k[0] == host]:
                    del cache[k]
            # A completed run also makes any cached analytics stale — clear the
            # (small) analytics cache wholesale so the next open recomputes.
            analytics = getattr(self, "_analytics_cache", None)
            if analytics is not None:
                analytics.clear()

    def _get_remote_history(
        self,
        entry: dict[str, Any],
        runnable: str | None,
        since_date: str | None = None,
    ) -> list[dict[str, Any]]:
        import time

        # Reuse a recent stream+parse for this (host, runnable) — the window is
        # applied *after* the cache so the bulk read (windowed) and get_run_detail
        # (unwindowed) share one entry. A miss-race just re-reads (same result).
        cache_key = (entry.get("name", ""), runnable or "")
        now = time.monotonic()
        with self._remote_history_lock:
            cached = self._remote_history_cache.get(cache_key)
        if cached is not None and (now - cached[0]) < self._REMOTE_HISTORY_TTL:
            records = cached[1]
        else:
            records = self._read_remote_history(entry, runnable)
            with self._remote_history_lock:
                self._remote_history_cache[cache_key] = (now, records)
        if since_date:
            records = [r for r in records if r.get("ts", "")[:10] >= since_date]
        return records[: self._HISTORY_CAP]

    def _read_remote_history(
        self, entry: dict[str, Any], runnable: str | None
    ) -> list[dict[str, Any]]:
        ssh = entry["ssh"]
        idf = entry.get("identityFile")
        paths = utils_mod._paths(entry)
        # Ask the remote runspec where its logs actually are (per venv), so a
        # non-standard install (pipx / ~/.local) whose logs the Logs tab finds
        # isn't missed by History. Union across venvs.
        log_dirs: list[str] = []
        for rp in paths:
            if not rp:
                continue
            for d in self._remote_log_dirs(entry, rp):
                if d not in log_dirs:
                    log_dirs.append(d)
        if not log_dirs:
            log_dirs = ["$HOME/logs"]
        # Stream only the newest ~cap files (a little headroom over the row cap
        # for single-mode/archive files that carry several runs) so the SSH
        # payload is bounded and can't be cut off by the timeout.
        from pathlib import PurePosixPath

        script = _remote_log_script(log_dirs, runnable, limit=self._HISTORY_CAP + 50)
        _, stdout, _ = self._ssh_run(ssh, script, identity_file=idf, timeout=20)
        # Tag remote records with the primary venv's group (best-effort — the
        # framed stream doesn't carry the source dir; correct in the common
        # single-venv case). Lets History's "View full log" drill resolve a venv.
        group = PurePosixPath(paths[0]).parent.parent.name if paths and paths[0] else ""
        records: list[dict[str, Any]] = []
        current_name: str | None = None
        current_lines: list[str] = []

        def _flush() -> None:
            if current_name is not None:
                for rec in _parse_log_text(
                    current_name, "\n".join(current_lines), entry["name"]
                ):
                    rec["group"] = group
                    records.append(rec)

        for line in stdout.splitlines():
            if line.startswith("\x00RUNSPEC_LOG:"):
                _flush()
                current_name = line[len("\x00RUNSPEC_LOG:") :]
                current_lines = []
            else:
                current_lines.append(line)
        _flush()
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return records[: self._HISTORY_CAP]

    # ── analytics ─────────────────────────────────────────────────────────────

    # Newest-N per-run files streamed per host for analytics — bounds a busy
    # host's payload (the date window trims further). Generous: normal fleets are
    # well under it; it only caps a pathological host.
    _ANALYTICS_FILE_CAP = 5000
    # Analytics result reuse window. The tab re-runs its scan on a 45 s poll and
    # on every scope/filter change; within this TTL a repeat request for the same
    # (hosts, window, runnable) is served from the last result instead of
    # re-streaming every host. Busted on run completion (_invalidate_remote_history).
    _ANALYTICS_TTL = 30.0

    def _collect_host_records(
        self, entry: dict[str, Any], runnable: str | None
    ) -> list[dict[str, Any]]:
        """Parse every run_summary across all of a host's venvs (uncapped).

        Unlike get_history this keeps the rich ``extra`` fields and tags each
        record with its venv ``group``. Local hosts read the log files directly;
        remote hosts stream them over SSH using the same framing as
        _get_remote_history. Raises on hard failure so the caller can mark the
        host partial.
        """
        from pathlib import PurePosixPath

        paths = utils_mod._paths(entry)
        records: list[dict[str, Any]] = []
        globs = _log_globs(runnable)

        if entry.get("ssh"):
            ssh = entry["ssh"]
            idf = entry.get("identityFile")
            # Every distinct dir across all venvs (path-derived + the shared
            # $HOME/logs), streamed in **one** SSH exec — not one exec per dir,
            # which was the per-dir round-trip that made Analytics slow. Capped to
            # the newest ~N files so a busy host's payload is bounded (Analytics
            # windows by date anyway). Records are tagged with the primary venv's
            # group (best-effort — the framed stream doesn't carry the source dir,
            # same as History's _read_remote_history).
            dirs: list[str] = []
            for rp in paths:
                if not rp:
                    continue
                for d in self._remote_log_dirs(entry, rp):
                    if d not in dirs:
                        dirs.append(d)
            if not dirs:
                dirs = ["$HOME/logs"]
            group = (
                PurePosixPath(paths[0]).parent.parent.name if paths and paths[0] else ""
            )
            script = _remote_log_script(dirs, runnable, limit=self._ANALYTICS_FILE_CAP)
            code, stdout, stderr = self._ssh_run(
                ssh, script, identity_file=idf, timeout=45
            )
            if code != 0 and not stdout:
                raise RuntimeError(stderr.strip() or f"ssh exit {code}")
            current_name: str | None = None
            current_lines: list[str] = []

            def _flush() -> None:
                if current_name is not None:
                    for rec in _parse_log_text(
                        current_name,
                        "\n".join(current_lines),
                        entry["name"],
                        include_extra=True,
                    ):
                        rec["group"] = group
                        records.append(rec)

            for line in stdout.splitlines():
                if line.startswith("\x00RUNSPEC_LOG:"):
                    _flush()
                    current_name = line[len("\x00RUNSPEC_LOG:") :]
                    current_lines = []
                else:
                    current_lines.append(line)
            _flush()
            return records

        # Local host (distinct Path var — `log_dir` above is a str for the
        # remote branch, so reusing the name would muddle its type). Scan each
        # venv's logs dir (tagged with its venv group) AND ~/logs (the writable
        # fallback when a venv dir isn't writable) so runs that landed there
        # aren't missed — same defect as get_history. ~/logs records are tagged
        # with the primary venv's group (correct in the common single-venv case).
        dir_groups: list[tuple[Path, str]] = [
            (Path(rp).parent.parent / "logs", hosts_mod.venv_name(rp))
            for rp in paths
            if rp
        ]
        fallback_group = hosts_mod.venv_name(paths[0]) if paths and paths[0] else ""
        dir_groups.append((Path.home() / "logs", fallback_group))
        seen_dirs: set[Path] = set()
        for local_dir, group in dir_groups:
            if local_dir in seen_dirs or not local_dir.exists():
                continue
            seen_dirs.add(local_dir)
            matches = {f for pat in globs for f in local_dir.glob(pat)}
            for log_file in matches:
                for rec in _parse_log(log_file, entry["name"], include_extra=True):
                    rec["group"] = group
                    records.append(rec)
        return records

    def get_analytics(
        self,
        hosts: list[str] | None = None,
        since_days: int = 30,
        runnable: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate run history across the fleet into drill-ready analytics.

        Scans full (uncapped) audit logs across the requested hosts' venvs,
        buckets by day and by every drill dimension, and surfaces the rich
        run_summary fields. Heavy work (parsing, SSH) happens here so the
        frontend can re-filter the compact ``buckets`` instantly.
        """
        import time
        from datetime import timezone

        if not hosts or "all" in hosts:
            target_names = ["local"] + [
                h["name"] for h in self._hosts if h.get("name") != "local"
            ]
        else:
            target_names = list(hosts)

        # Skip disconnected remotes — a down/slow host otherwise burns the full
        # TCP + banner connect timeout (10s + 30s) in the join, which is the
        # "fast on a few hosts, then a minute" stall. `local` is always readable.
        with self._lock:
            connected = dict(self._connected_cache)
        target_names = [
            n for n in target_names if n == "local" or connected.get(n, False)
        ]

        # Serve a recent identical scan (same hosts/window/runnable) from cache so
        # the 45 s poll and rapid filter changes don't re-stream every host.
        cache_key = (tuple(target_names), int(since_days), runnable or "")
        now_mono = time.monotonic()
        with self._remote_history_lock:
            hit = self._analytics_cache.get(cache_key)
        if hit is not None and (now_mono - hit[0]) < self._ANALYTICS_TTL:
            return hit[1]

        entries: list[dict[str, Any]] = []
        for name in target_names:
            e = self._host_entry(name)
            if e is not None:
                entries.append(e)

        now = datetime.now(timezone.utc)
        since_dt = now - timedelta(days=max(0, since_days - 1))
        since = since_dt.date().isoformat()
        until = now.date().isoformat()

        all_records: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        lock = threading.Lock()

        def collect(entry: dict[str, Any]) -> None:
            try:
                recs = self._collect_host_records(entry, runnable)
            except Exception as exc:  # noqa: BLE001 — degrade gracefully per host
                with lock:
                    errors.append({"host": entry["name"], "message": str(exc)})
                return
            with lock:
                all_records.extend(recs)

        threads = [
            threading.Thread(target=collect, args=(e,), daemon=True) for e in entries
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Window filter (UTC day, inclusive lower bound)
        in_window = [r for r in all_records if r.get("ts", "")[:10] >= since]

        result = _aggregate_analytics(
            in_window, since, until, partial=bool(errors), errors=errors
        )
        # Cache only a clean (non-partial) result — a partial one should re-try
        # next open rather than pin a host's transient failure for the whole TTL.
        if not errors:
            with self._remote_history_lock:
                self._analytics_cache[cache_key] = (now_mono, result)
        return result

    # ── logs management ───────────────────────────────────────────────────────
    #
    # The Logs tab drives `runspec logs <verb> --json` against each venv — local
    # via subprocess, remote via SSH — the same uniform, version-correct
    # interface discovery uses (`runspec local`). status is read-only inventory;
    # prune/compact mutate, so the UI gates them behind a --dry-run preview.

    def logs_status(
        self, host: str, runnable: str | None = None
    ) -> list[dict[str, Any]]:
        """Per-scope per-runnable log inventory for a host (read-only).

        One entry per venv (its own ``{sys.prefix}/logs``), plus a single
        ``~/logs`` "home" entry when the shared fallback dir holds any logs. Each
        is ``{group, ok, dirs, runnables, total_bytes, total_files}`` (or
        ``{group, ok: False, error}`` on failure).

        Built from **one directory listing per host** — a local ``scandir`` or a
        single ``find`` over SSH — and classified in-process via ``runspec.logs``.
        Replaces the old per-venv ``runspec logs status --json`` call, which
        cold-started a full remote ``runspec`` process per venv (serially) and
        made this tab slow out of all proportion to the tiny data it reports.
        """
        scopes = self._log_scopes(host)
        if not scopes:
            return []
        entry = self._host_entry(host)
        is_remote = entry is not None and bool(entry.get("ssh"))
        try:
            if entry is not None and entry.get("ssh"):
                rows_by_group = self._remote_log_listing(entry, scopes)
            else:
                rows_by_group = self._local_log_listing(scopes)
        except Exception as exc:  # noqa: BLE001 — one bad listing shouldn't blank the tab
            logger.exception("logs_status: listing failed for %s", host)
            return [
                {"group": g, "ok": False, "error": str(exc)}
                for (g, kind, _rp) in scopes
                if kind != "home"
            ]
        out: list[dict[str, Any]] = []
        for group, kind, rp in scopes:
            rows = rows_by_group.get(group, [])
            if kind == "home" and not rows:
                continue  # only surface ~/logs when the shared fallback holds logs
            # Mirror the listing's own path handling: the display dir is native
            # for the local host, POSIX for a remote fleet host.
            dir_display = (
                "~/logs"
                if kind == "home"
                else str(self._venv_logdir(rp, remote=is_remote))
            )
            out.append(self._build_status_entry(group, [dir_display], rows, runnable))
        return out

    # ── log inventory internals (one listing per host, no remote runspec) ────────

    @staticmethod
    def _venv_logdir(rp: str, *, remote: bool = True) -> PurePath:
        """A venv's own logs dir, path-derived from its runspec binary:
        ``{venv}/bin/runspec`` → ``{venv}/logs`` (== ``{sys.prefix}/logs``).

        Remote fleet paths are always POSIX (SSH), so parse them with
        ``PurePosixPath``. The **local** host's path is native — parse it with
        ``Path`` so a Windows console derives ``C:\\…\\logs`` instead of
        collapsing to a bare ``logs`` (a backslash path is a single segment to
        ``PurePosixPath``, so ``.parent.parent`` would drop to ``.``)."""
        base: PurePath = PurePosixPath(rp) if remote else Path(rp)
        return base.parent.parent / "logs"

    def _log_scopes(self, host: str) -> list[tuple[str, str, str]]:
        """``[(group, kind, rp)]`` for the Logs tab: one per venv (kind
        ``"venv"``) then the shared home fallback (kind ``"home"``,
        ``_HOME_SCOPE``, driven by the primary venv's binary). Empty when the host
        has no venvs."""
        venvs = self._venvs(host)
        scopes: list[tuple[str, str, str]] = [(g, "venv", rp) for g, rp in venvs]
        if venvs:
            scopes.append((_HOME_SCOPE, "home", venvs[0][1]))
        return scopes

    def _local_log_listing(
        self, scopes: list[tuple[str, str, str]]
    ) -> dict[str, list[tuple[str, int, float]]]:
        """In-process listing of each scope's dir on the local host — no subprocess."""
        out: dict[str, list[tuple[str, int, float]]] = {}
        for group, kind, rp in scopes:
            d = (
                Path.home() / "logs"
                if kind == "home"
                else Path(rp).parent.parent / "logs"
            )
            rows: list[tuple[str, int, float]] = []
            try:
                with os.scandir(d) as it:
                    for e in it:
                        n = e.name
                        if not (n.endswith(".log") or n.endswith(".log.gz")):
                            continue
                        try:
                            if not e.is_file():
                                continue
                            st = e.stat()
                        except OSError:
                            continue
                        rows.append((n, st.st_size, st.st_mtime))
            except OSError:
                pass  # missing/unreadable dir → empty inventory for this scope
            out[group] = rows
        return out

    def _remote_log_listing(
        self, entry: dict[str, Any], scopes: list[tuple[str, str, str]]
    ) -> dict[str, list[tuple[str, int, float]]]:
        """One SSH ``find`` over every scope's dir, framed per scope — no remote
        ``runspec`` process. ``-printf '%f\\t%s\\t%T@'`` gives basename, size and
        mtime; classification happens in the console (``runspec.logs``)."""
        blocks: list[str] = []
        for group, kind, rp in scopes:
            dshell = (
                '"$HOME/logs"'
                if kind == "home"
                else shlex.quote(str(self._venv_logdir(rp)))
            )
            blocks.append(
                "printf '\\x00RSSCOPE:%s\\n' " + shlex.quote(group) + "; "
                "find " + dshell + " -maxdepth 1 -type f "
                r"\( -name '*.log' -o -name '*.log.gz' \) "
                "-printf '%f\\t%s\\t%T@\\n' 2>/dev/null"
            )
        script = "; ".join(blocks)
        _, stdout, _ = self._ssh_run(
            entry["ssh"], script, identity_file=entry.get("identityFile"), timeout=30
        )
        out: dict[str, list[tuple[str, int, float]]] = {
            g: [] for (g, _k, _rp) in scopes
        }
        current: str | None = None
        for line in stdout.splitlines():
            if line.startswith("\x00RSSCOPE:"):
                current = line[len("\x00RSSCOPE:") :]
                out.setdefault(current, [])
                continue
            if current is None:
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            name, size_s, mtime_s = parts
            try:
                out[current].append((name, int(size_s), float(mtime_s)))
            except ValueError:
                continue
        return out

    def _build_status_entry(
        self,
        group: str,
        dirs: list[str],
        rows: list[tuple[str, int, float]],
        runnable: str | None,
    ) -> dict[str, Any]:
        """Fold a scope's ``(name, size, mtime)`` rows into the status shape the
        Logs tab renders. Single-mode ``{runnable}.log`` files are never counted
        (only per-run files and archives — same as ``runspec logs status``)."""
        per: dict[str, dict[str, Any]] = {}
        for name, size, mtime in rows:
            kind = core_logs.classify_name(name)
            if kind == "single":
                continue
            rn = core_logs.runnable_of(name)
            if runnable and rn != runnable:
                continue
            r = per.setdefault(
                rn,
                {
                    "runnable": rn,
                    "per_run_files": 0,
                    "archives": 0,
                    "total_bytes": 0,
                    "_o": None,
                    "_n": None,
                },
            )
            if kind == "archive":
                r["archives"] += 1
            else:
                r["per_run_files"] += 1
            r["total_bytes"] += size
            if r["_o"] is None or mtime < r["_o"]:
                r["_o"] = mtime
            if r["_n"] is None or mtime > r["_n"]:
                r["_n"] = mtime
        runnables = [
            {
                "runnable": r["runnable"],
                "per_run_files": r["per_run_files"],
                "archives": r["archives"],
                "total_bytes": r["total_bytes"],
                "oldest": _mtime_iso(r["_o"]) if r["_o"] is not None else None,
                "newest": _mtime_iso(r["_n"]) if r["_n"] is not None else None,
            }
            for r in sorted(per.values(), key=lambda x: x["runnable"])
        ]
        return {
            "group": group,
            "ok": True,
            "dirs": dirs,
            "runnables": runnables,
            "total_bytes": sum(r["total_bytes"] for r in runnables),
            "total_files": sum(r["per_run_files"] + r["archives"] for r in runnables),
        }

    def logs_view(
        self,
        host: str,
        group: str,
        runnable: str,
        since: str | None = None,
        user: str | None = None,
        run: str | None = None,
    ) -> dict[str, Any]:
        """Merged per-invocation stream for one runnable in one venv.

        Returns ``{ok, records}`` where each record is a parsed JSON log line
        (timestamp-sorted, archives included). Drives ``runspec logs <runnable>
        --json``.
        """
        rp, scope = self._scope_for(host, group)
        if not rp:
            return {"ok": False, "error": f"no venv '{group}' on {host}", "records": []}
        argv = ["logs", runnable, "--json", "--scope", scope]
        if since:
            argv += ["--since", since]
        if user:
            argv += ["--user", user]
        if run:
            argv += ["--run", run]
        code, stdout, stderr = self._run_logs(host, rp, argv, timeout=45)
        if code != 0 and not stdout.strip():
            return {
                "ok": False,
                "error": stderr.strip() or f"exit {code}",
                "records": [],
            }
        records: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return {"ok": True, "records": records}

    def logs_prune(
        self,
        host: str,
        group: str,
        runnable: str | None = None,
        older_than: str | None = None,
        max_files: int | None = None,
        max_total_size: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Prune old per-invocation files in one venv. Drives ``runspec logs prune``.

        At least one policy is required. Returns the engine's structured result
        (``{dry_run, count, freed_bytes, deleted}``) plus ``{group, ok}``.
        """
        if older_than is None and max_files is None and max_total_size is None:
            return {
                "ok": False,
                "error": "prune needs at least one policy (--older-than / --max-files / --max-total-size)",
            }
        rp, scope = self._scope_for(host, group)
        if not rp:
            return {"ok": False, "error": f"no venv '{group}' on {host}"}
        argv = ["logs", "prune"]
        if runnable:
            argv.append(runnable)
        if older_than:
            argv += ["--older-than", older_than]
        if max_files is not None:
            argv += ["--max-files", str(max_files)]
        if max_total_size:
            argv += ["--max-total-size", max_total_size]
        if dry_run:
            argv.append("--dry-run")
        # Scope the delete to just this entry's dir (a venv's own {sys.prefix}/logs,
        # or ~/logs for the home entry) — never both — so pruning one venv can't
        # delete another venv's files in the shared ~/logs. Needs runspec >= 0.56
        # on the host; an older one fails the arg safely (no deletion, see
        # _logs_action's hint).
        argv += ["--scope", scope]
        argv.append("--json")
        return self._logs_action(host, rp, group, argv, timeout=120)

    def logs_compact(
        self,
        host: str,
        group: str,
        runnable: str | None = None,
        older_than: str | None = None,
        gzip: bool = True,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Compact old per-invocation files into archives in one venv.

        ``--older-than`` is required. Returns the engine's structured result
        (``{dry_run, compacted, archives}``) plus ``{group, ok}``.
        """
        if not older_than:
            return {"ok": False, "error": "compact requires --older-than (e.g. 7d)"}
        rp, scope = self._scope_for(host, group)
        if not rp:
            return {"ok": False, "error": f"no venv '{group}' on {host}"}
        argv = ["logs", "compact"]
        if runnable:
            argv.append(runnable)
        argv += ["--older-than", older_than]
        if gzip:
            argv.append("--gzip")
        if dry_run:
            argv.append("--dry-run")
        argv += ["--scope", scope]  # this entry's dir only (see logs_prune)
        argv.append("--json")
        return self._logs_action(host, rp, group, argv, timeout=180)

    def _logs_action(
        self, host: str, rp: str, group: str, argv: list[str], timeout: int
    ) -> dict[str, Any]:
        """Run a prune/compact action and merge its JSON result with {group, ok}."""
        code, stdout, stderr = self._run_logs(host, rp, argv, timeout=timeout)
        if code != 0 and not stdout.strip():
            err = stderr.strip() or f"exit {code}"
            # A pre-0.56 runspec on the host doesn't know --scope and fails the arg
            # (safely — nothing deleted). Point the operator at the fix rather than
            # leaving a cryptic "unknown argument" (and never fall back to an
            # unscoped prune, which is the data-loss bug we're fixing).
            if "scope" in err.lower():
                err += (
                    " — the runspec on this host is likely older than 0.56, which "
                    "can't scope a prune to one venv; upgrade it to prune from the "
                    "console."
                )
            return {"group": group, "ok": False, "error": err}
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return {"group": group, "ok": False, "error": "unparseable result output"}
        payload["group"] = group
        payload["ok"] = True
        return payload

    def _venvs(self, host: str) -> list[tuple[str, str]]:
        """(group, runspec_path) for every venv on a host."""
        entry = self._host_entry(host)
        if entry is None:
            return []
        return [(hosts_mod.venv_name(rp), rp) for rp in utils_mod._paths(entry) if rp]

    def _venv_path(self, host: str, group: str) -> str:
        """Resolve a host+venv group to its runspec binary path, or ''."""
        return next((rp for g, rp in self._venvs(host) if g == group), "")

    def _scope_for(self, host: str, group: str) -> tuple[str, str]:
        """Resolve a Logs-tab ``group`` to ``(runspec_path, scope)`` for a scoped
        ``runspec logs`` call.

        A venv group → that venv's binary + ``--scope venv`` (acts on
        ``{sys.prefix}/logs`` only — never the shared ~/logs, so pruning one venv
        can't delete another's files). The ``_HOME_SCOPE`` entry → the primary
        venv's binary + ``--scope home`` (acts on ~/logs only). ``("", "")`` when
        the group can't be resolved.
        """
        if group == _HOME_SCOPE:
            venvs = self._venvs(host)
            return (venvs[0][1], "home") if venvs else ("", "")
        return (self._venv_path(host, group), "venv")

    def _run_logs(
        self, host: str, rp: str, argv: list[str], timeout: int
    ) -> tuple[int, str, str]:
        """Run ``<rp> <argv...>`` on a venv — local subprocess or remote SSH.

        Mirrors discovery: local hosts shell out directly; remote hosts go over
        SSH with the path + args joined into a quoted command string.
        """
        import shlex

        entry = self._host_entry(host)
        if entry is None:
            return -1, "", f"unknown host {host}"

        if entry.get("ssh"):
            command = " ".join(shlex.quote(p) for p in [rp, *argv])
            return self._ssh_run(
                entry["ssh"],
                command,
                identity_file=entry.get("identityFile"),
                timeout=timeout,
            )

        import subprocess

        from .procutil import no_window_kwargs

        try:
            proc = subprocess.run(
                [rp, *argv],
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                **no_window_kwargs(),
            )
        except FileNotFoundError:
            return -1, "", f"runspec not found at {rp}"
        except subprocess.TimeoutExpired:
            return -1, "", f"timed out after {timeout}s"
        return proc.returncode, proc.stdout, proc.stderr

    # ── schedules ─────────────────────────────────────────────────────────────

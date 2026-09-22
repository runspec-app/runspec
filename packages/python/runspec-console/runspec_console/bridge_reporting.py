"""bridge_reporting.py — ReportingMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


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


class ReportingMixin:
    # Built-in agent-tool calls (repo_*, outlook_*, manage_denylist, action and
    # memory tools) leave no run_summary the way a runnable does, so they used to
    # be absent from History/Analytics. We write one run_summary-shaped JSON line
    # per call to ~/logs/runspec-console-tools.log — a "local"-host log dir the
    # history scanner already reads (*.log glob) — so they surface with no parser
    # change. Attribution comes from the record body (extra.runnable = the tool
    # label), not the filename.
    _TOOL_AUDIT_FILE = "runspec-console-tools.log"

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
        try:
            log_dir = Path.home() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, default=str) + "\n"
            with self._audit_lock:
                with (log_dir / self._TOOL_AUDIT_FILE).open(
                    "a", encoding="utf-8"
                ) as fh:
                    fh.write(line)
        except Exception:  # noqa: BLE001 — auditing is best-effort
            logger.warning("could not write built-in tool audit line", exc_info=True)

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
        try:
            log_dir = Path.home() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, default=str) + "\n"
            with self._audit_lock:
                with (log_dir / self._TOOL_AUDIT_FILE).open(
                    "a", encoding="utf-8"
                ) as fh:
                    fh.write(line)
        except Exception:  # noqa: BLE001 — auditing is best-effort
            logger.warning("could not write chat-turn audit line", exc_info=True)

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
        try:
            log_dir = Path.home() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, default=str) + "\n"
            with self._audit_lock:
                with (log_dir / self._TOOL_AUDIT_FILE).open(
                    "a", encoding="utf-8"
                ) as fh:
                    fh.write(line)
        except Exception:  # noqa: BLE001 — auditing is best-effort
            logger.warning("could not write disclaimer audit line", exc_info=True)

    # Cap on rows the History tab shows. Applied newest-first, and used as the
    # early-stop target so we never read more files than needed to fill it.
    _HISTORY_CAP = 200

    def get_history(
        self, host: str, runnable: str | None = None
    ) -> list[dict[str, Any]]:
        entry = self._host_entry(host)
        if entry is None:
            return []
        if entry.get("ssh"):
            return self._get_remote_history(entry, runnable)
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
        records = self._read_newest(files, host)
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        self._prune_history_cache({str(p) for p, *_ in files})
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

    def get_history_all(self) -> list[dict[str, Any]]:
        """History across every host — local plus *connected* remotes — newest
        first. Disconnected remotes are skipped so a slow/offline host can't hang
        the read (the History tab uses this so a run shows up regardless of which
        host it ran on; per-host scoping stays for Forms/Logs/etc.).

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
                return self.get_history(name)
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
        return records[:300]

    def _remote_log_dirs(
        self, entry: dict[str, Any], rp: str, timeout: int = 30
    ) -> list[str]:
        """Real log directories for one remote venv, asked of the remote runspec.

        ``logs status --json`` reports ``log_dirs()`` — the actual dirs the Logs
        tab reads — so History/Analytics don't guess the dir from the binary
        path (which is wrong for pipx / ~/.local / system installs, where the
        runnable's ``sys.prefix`` isn't ``{rp}/../..``). Falls back to the
        path-derived dir plus ``$HOME/logs``. Cached per venv path for the
        session (only when the query actually resolved, so a transient failure
        doesn't pin the fallback)."""
        from pathlib import PurePosixPath
        import shlex

        cached = self._remote_logdir_cache.get(rp)
        if cached is not None:
            return cached

        dirs: list[str] = []

        def _add(d: str) -> None:
            if d and d not in dirs:
                dirs.append(d)

        resolved = False
        cmd = " ".join(shlex.quote(p) for p in [rp, "logs", "status", "--json"])
        try:
            code, out, _ = self._ssh_run(
                entry["ssh"],
                cmd,
                identity_file=entry.get("identityFile"),
                timeout=timeout,
            )
            if code == 0 and out.strip():
                for d in json.loads(out).get("dirs", []):
                    _add(str(d))
                resolved = True
        except Exception:
            logger.debug("remote log-dir query failed for %s", rp, exc_info=True)
        _add(str(PurePosixPath(rp).parent.parent / "logs"))  # path-derived fallback
        _add("$HOME/logs")
        if resolved:
            self._remote_logdir_cache[rp] = dirs
        return dirs

    def _get_remote_history(
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
            # Ask the remote runspec for each venv's real log dirs (same as
            # History) so non-standard installs aren't missed, tagging each dir
            # with that venv's group.
            remote_dir_groups: list[tuple[str, str]] = []
            for rp in paths:
                if not rp:
                    continue
                group = PurePosixPath(rp).parent.parent.name
                for d in self._remote_log_dirs(entry, rp):
                    remote_dir_groups.append((d, group))
            seen_remote: set[str] = set()
            for log_dir, group in remote_dir_groups:
                if log_dir in seen_remote:
                    continue
                seen_remote.add(log_dir)
                script = _remote_log_script(log_dir, runnable)
                code, stdout, stderr = self._ssh_run(
                    ssh, script, identity_file=idf, timeout=45
                )
                if code != 0 and not stdout:
                    raise RuntimeError(stderr.strip() or f"ssh exit {code}")
                current_name: str | None = None
                current_lines: list[str] = []

                def _flush(group: str = group) -> None:
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
        from datetime import timezone

        if not hosts or "all" in hosts:
            target_names = ["local"] + [
                h["name"] for h in self._hosts if h.get("name") != "local"
            ]
        else:
            target_names = list(hosts)

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

        return _aggregate_analytics(
            in_window, since, until, partial=bool(errors), errors=errors
        )

    # ── logs management ───────────────────────────────────────────────────────
    #
    # The Logs tab drives `runspec logs <verb> --json` against each venv — local
    # via subprocess, remote via SSH — the same uniform, version-correct
    # interface discovery uses (`runspec local`). status is read-only inventory;
    # prune/compact mutate, so the UI gates them behind a --dry-run preview.

    def logs_status(
        self, host: str, runnable: str | None = None
    ) -> list[dict[str, Any]]:
        """Per-venv per-runnable log inventory for a host (read-only).

        Returns one dict per venv: ``{group, ok, dirs, runnables, total_bytes,
        total_files}`` (or ``{group, ok: False, error}`` on failure). Drives
        ``runspec logs status --json``.
        """
        argv = ["logs", "status"]
        if runnable:
            argv.append(runnable)
        argv.append("--json")

        out: list[dict[str, Any]] = []
        for group, rp in self._venvs(host):
            code, stdout, stderr = self._run_logs(host, rp, argv, timeout=30)
            if code != 0 and not stdout.strip():
                out.append(
                    {
                        "group": group,
                        "ok": False,
                        "error": stderr.strip() or f"exit {code}",
                    }
                )
                continue
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                out.append(
                    {"group": group, "ok": False, "error": "unparseable status output"}
                )
                continue
            payload["group"] = group
            payload["ok"] = True
            out.append(payload)
        return out

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
        rp = self._venv_path(host, group)
        if not rp:
            return {"ok": False, "error": f"no venv '{group}' on {host}", "records": []}
        argv = ["logs", runnable, "--json"]
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
        rp = self._venv_path(host, group)
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
        rp = self._venv_path(host, group)
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
        argv.append("--json")
        return self._logs_action(host, rp, group, argv, timeout=180)

    def _logs_action(
        self, host: str, rp: str, group: str, argv: list[str], timeout: int
    ) -> dict[str, Any]:
        """Run a prune/compact action and merge its JSON result with {group, ok}."""
        code, stdout, stderr = self._run_logs(host, rp, argv, timeout=timeout)
        if code != 0 and not stdout.strip():
            return {
                "group": group,
                "ok": False,
                "error": stderr.strip() or f"exit {code}",
            }
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

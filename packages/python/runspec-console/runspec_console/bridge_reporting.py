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
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class ReportingMixin:
    def get_history(
        self, host: str, runnable: str | None = None
    ) -> list[dict[str, Any]]:
        entry = self._host_entry(host)
        if entry is None:
            return []
        if entry.get("ssh"):
            return self._get_remote_history(entry, runnable)
        paths = utils_mod._paths(entry)
        records: list[dict[str, Any]] = []
        globs = _log_globs(runnable)
        # Scan every venv's logs dir AND ~/logs. A runnable writes to ~/logs when
        # its venv dir isn't writable (managed machines) — but the venv dir is
        # still *created* on the way to that fallback, so a "venv dir, else
        # ~/logs" check reads an empty venv dir and misses the real records in
        # ~/logs. The Logs tab works because the CLI's log_dirs() reads both.
        log_dirs = [Path(rp).parent.parent / "logs" for rp in paths if rp]
        log_dirs.append(Path.home() / "logs")
        matches: set[Path] = set()
        seen_dirs: set[Path] = set()
        for log_dir in log_dirs:
            if log_dir in seen_dirs or not log_dir.exists():
                continue
            seen_dirs.add(log_dir)
            matches |= {f for pat in globs for f in log_dir.glob(pat)}
        for log_file in sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True):
            records.extend(_parse_log(log_file, host))
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return records[:200]

    def get_history_all(self) -> list[dict[str, Any]]:
        """History across every host — local plus *connected* remotes — newest
        first. Disconnected remotes are skipped so a slow/offline host can't hang
        the read (the History tab uses this so a run shows up regardless of which
        host it ran on; per-host scoping stays for Forms/Logs/etc.)."""
        with self._lock:
            remotes = list(self._hosts)
            connected = dict(self._connected_cache)
        names = ["local"]
        names += [
            h["name"] for h in remotes if h.get("name") and connected.get(h["name"])
        ]
        records: list[dict[str, Any]] = []
        for name in names:
            try:
                records.extend(self.get_history(name))
            except Exception:
                logger.exception("history: read failed for host %s", name)
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
        script = _remote_log_script(log_dirs, runnable)
        _, stdout, _ = self._ssh_run(ssh, script, identity_file=idf, timeout=20)
        records: list[dict[str, Any]] = []
        current_name: str | None = None
        current_lines: list[str] = []
        for line in stdout.splitlines():
            if line.startswith("\x00RUNSPEC_LOG:"):
                if current_name is not None:
                    records.extend(
                        _parse_log_text(
                            current_name, "\n".join(current_lines), entry["name"]
                        )
                    )
                current_name = line[len("\x00RUNSPEC_LOG:") :]
                current_lines = []
            else:
                current_lines.append(line)
        if current_name is not None:
            records.extend(
                _parse_log_text(current_name, "\n".join(current_lines), entry["name"])
            )
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return records[:200]

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

        try:
            proc = subprocess.run(
                [rp, *argv],
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            return -1, "", f"runspec not found at {rp}"
        except subprocess.TimeoutExpired:
            return -1, "", f"timed out after {timeout}s"
        return proc.returncode, proc.stdout, proc.stderr

    # ── schedules ─────────────────────────────────────────────────────────────

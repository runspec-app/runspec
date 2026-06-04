"""
bridge.py — Python implementation of the BridgeApi declared in bridge/index.ts.

Every public method on this class is callable from the frontend via
window.pywebview.api.<method>(...).  pywebview wraps each call in a Promise
automatically — methods must be synchronous (no async def).

Streaming (invoke_runnable, send_chat) works by dispatching CustomEvents from
background threads via window.evaluate_js().  The frontend listens for:
  runspec:output   { id, line, stream }          — one log line
  runspec:run_end  { id, exit_code, duration_ms } — invocation finished
  runspec:token    { id, token }                  — LLM token (chat only)
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import random
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from .config import hosts_path, read_config, write_config
from .discovery import discover_local, discover_remote
from .executor import run_local, run_remote
from .hosts import load_hosts, save_hosts, venv_name

logger = logging.getLogger(__name__)

_CANCEL_KEY = "__cancel_event__"  # key in _in_flight dicts, not surfaced to JS


class _ParamikoDevHandler(logging.Handler):
    """Forward paramiko log records into the Dev tab as ``runspec:ssh`` events.

    paramiko logs the full "Error reading SSH protocol banner" traceback from
    its transport thread at ERROR. With no handler configured those records hit
    Python's last-resort handler and spill into the launching terminal as raw
    tracebacks. Routing them here turns each into a filterable, timestamped
    Dev-tab row (the connect/banner/auth lifecycle, with the exact failure), and
    — because the paramiko logger's ``propagate`` is turned off when this is
    installed — keeps the terminal clean.
    """

    def __init__(self, bridge: Bridge) -> None:
        super().__init__()
        self._bridge = bridge
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            detail: dict[str, Any] = {
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            # format() appends the traceback after the (bare) message when the
            # record carries exc_info — that's the banner-timeout stack we want
            # expandable in the Dev tab.
            if record.exc_info or record.exc_text:
                detail["traceback"] = self.format(record)
            self._bridge._emit_ssh_log(detail)
        except Exception:
            # A logging handler must never raise — that would break the call
            # site that emitted the log (here, paramiko's transport thread).
            pass


# Autonomy escalation order — higher = more restrictive (must check before run).
_AUTONOMY_RANK = {"autonomous": 0, "confirm": 1, "supervised": 2, "manual": 3}


class Bridge:
    def __init__(self) -> None:
        self._window: Any = None
        self._debug: bool = False
        self._lock = threading.Lock()
        self._in_flight: dict[str, dict[str, Any]] = {}
        self._adapter: Any = None  # ModelAdapter, loaded on demand
        self._hosts: list[dict[str, Any]] = []
        self._connected_cache: dict[str, bool] = {}  # host name → last known state
        self._runnables_cache: list[dict[str, Any]] = []
        # Persistent agent conversation so the model remembers prior turns. One
        # rolling conversation per app session; reset via clear_chat(). The lock
        # serialises turns so two quick sends can't interleave the history.
        self._chat_history: list[dict[str, Any]] = []
        self._chat_lock = threading.Lock()
        # chat_id → cancel Event, so a turn can be stopped mid-run (guarded by
        # self._lock, NOT _chat_lock — cancel must fire while a turn holds that).
        self._chat_cancels: dict[str, threading.Event] = {}
        # request_id → (chat_id, Event, {"approved", "cancelled"}) for agent
        # tool-call confirmations. chat_id lets cancel_chat wake a pending
        # prompt so Stop works while the Approve/Deny dialog is up.
        self._pending_confirms: dict[
            str, tuple[str, threading.Event, dict[str, bool]]
        ] = {}
        self._ssh_log_handler: _ParamikoDevHandler | None = None
        # SSH log records captured before the window attaches are buffered here
        # (not dropped), then flushed to the Dev tab once it's ready — so the
        # very first refresh cycle's connect logs aren't lost.
        self._pre_window_ssh_logs: list[dict[str, Any]] = []
        self._refresh_started = False
        # Centralized SSH connection pool — one reused transport per host,
        # shared by every SSH path (refresh, discovery, history, logs,
        # invocation). None when [ssh] pool = false (legacy connect-per-call).
        self._ssh_pool: Any = self._build_ssh_pool()
        self._install_ssh_log_forwarder()
        self._reload_hosts()
        import atexit

        atexit.register(self._close_ssh_pool)

    def set_window(self, window: Any) -> None:
        self._window = window
        # Flush any SSH logs captured during pre-window startup, then start the
        # background refresh. Starting it here (not in __init__) means the first
        # cycle never dispatches into a missing window — which is what leaked the
        # "dispatch dropped: no window attached" warnings to the terminal.
        with self._lock:
            buffered = self._pre_window_ssh_logs
            self._pre_window_ssh_logs = []
        for detail in buffered:
            self._dispatch("runspec:ssh", detail)
        if not self._refresh_started:
            self._refresh_started = True
            self._start_refresh_watcher()

    def _build_ssh_pool(self) -> Any:
        """Construct the SSH connection pool from current ``[ssh]`` config.

        Returns None when pooling is disabled — callers then fall back to the
        connect-per-call ``executor.ssh_run`` path.
        """
        from .ssh_pool import SSHConnectionPool, pool_settings
        from .executor import _make_ssh_client

        settings = pool_settings(self.get_config().get("ssh"))
        if not settings["enabled"]:
            return None
        return SSHConnectionPool(
            settings=settings,
            make_client=_make_ssh_client,
            on_event=self._on_pool_event,
        )

    def _on_pool_event(self, detail: dict[str, Any]) -> None:
        """Surface a pool-lifecycle event in the Dev tab's ``ssh`` category.

        DEBUG rows (per-cycle reuse / new handshake / reap) are chatty, so they
        appear only when started with ``--dev`` — the same gate the paramiko
        capture uses. INFO/WARNING (reconnect, connect-backoff) always show.
        """
        if detail.get("level") == "DEBUG" and not self._debug:
            return
        self._emit_ssh_log(detail)

    def _close_ssh_pool(self) -> None:
        pool = self._ssh_pool
        if pool is not None:
            pool.close_all()

    def _ssh_run(
        self,
        ssh_target: str,
        command: str,
        *,
        identity_file: str | None = None,
        timeout: int = 15,
    ) -> tuple[int, str, str]:
        """Run one SSH command, reusing a pooled transport when pooling is on.

        Single chokepoint for the bridge's one-shot SSH commands (discovery,
        history, logs, analytics) so they all share the per-host connection.
        Falls back to ``executor.ssh_run`` when the pool is disabled.
        """
        cfg = self.get_config().get("ssh")
        pool = self._ssh_pool
        if pool is not None:
            return pool.run(
                ssh_target,
                command,
                identity_file=identity_file,
                global_ssh_config=cfg,
                timeout=timeout,
            )
        from .executor import ssh_run

        return ssh_run(
            ssh_target,
            command,
            identity_file=identity_file,
            global_ssh_config=cfg,
            timeout=timeout,
        )

    def set_debug(self, debug: bool) -> None:
        """Record whether the Chromium inspector was enabled at startup.

        Also widens SSH log capture to DEBUG when debug is on, so the Dev tab's
        ``ssh`` rows include paramiko's packet/kex detail for deep investigation.
        """
        self._debug = debug
        logging.getLogger("paramiko").setLevel(logging.DEBUG if debug else logging.INFO)

    def _install_ssh_log_forwarder(self) -> None:
        """Route paramiko's loggers into the Dev tab and off the terminal.

        Attaches one handler to the top ``paramiko`` logger (child loggers such
        as ``paramiko.transport`` propagate up to it) and disables propagation
        so records never reach the root logger / launching terminal.
        """
        handler = _ParamikoDevHandler(self)
        self._ssh_log_handler = handler
        root = logging.getLogger("paramiko")
        # Idempotent: drop any handler we installed on a previous construction
        # (matters under tests / repeated Bridge() in one process).
        root.handlers = [
            h for h in root.handlers if not isinstance(h, _ParamikoDevHandler)
        ]
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        root.propagate = False
        # Let paramiko.transport inherit the parent level so its records flow to
        # the shared handler at the level we set above (app._suppress_noisy_loggers
        # pins it to WARNING at startup; clear that so INFO connect logs surface).
        logging.getLogger("paramiko.transport").setLevel(logging.NOTSET)

    def _emit_ssh_log(self, detail: dict[str, Any]) -> None:
        """Dispatch a captured paramiko record as a ``runspec:ssh`` event.

        Before the window attaches, buffer the record rather than dropping it —
        set_window() flushes the buffer so early connect logs still reach the
        Dev tab. The buffer is bounded so a connect storm before the UI is up
        can't grow it without limit.
        """
        if self._window is None:
            with self._lock:
                if len(self._pre_window_ssh_logs) < 200:
                    self._pre_window_ssh_logs.append(detail)
            return
        self._dispatch("runspec:ssh", detail)

    def is_debug_enabled(self) -> bool:
        """Exposed to the Dev tab so its inspector button behaves honestly."""
        return self._debug

    def open_devtools(self) -> None:
        """Best-effort open of the native Chromium inspector.

        pywebview 5.x has no reliable cross-platform API for this — debug mode
        (set at startup) enables right-click → Inspect / F12. We attempt any hook
        the backend happens to expose and otherwise no-op; the Dev tab tells the
        user to use the context menu / F12.
        """
        win = self._window
        if win is None:
            return
        for attr in ("open_devtools", "show_devtools"):
            fn = getattr(win, attr, None)
            if callable(fn):
                try:
                    fn()
                    return
                except Exception:
                    logger.debug("%s failed", attr, exc_info=True)

    # ── window controls ───────────────────────────────────────────────────────

    def minimize_window(self) -> None:
        if self._window:
            self._window.minimize()

    def toggle_maximize_window(self) -> None:
        if self._window:
            if self._window.maximized:
                self._window.restore()
            else:
                self._window.maximize()

    def close_window(self) -> None:
        if self._window:
            self._window.destroy()

    def resize_window(self, width: int, height: int) -> None:
        if self._window:
            self._window.resize(width, height)

    def move_window(self, x: int, y: int) -> None:
        if self._window:
            self._window.move(x, y)

    # ── hosts ────────────────────────────────────────────────────────────────

    def get_hosts(self) -> list[dict[str, Any]]:
        self._reload_hosts()
        local = self._local_host_entry()
        all_hosts = [local] + [h for h in self._hosts if h.get("name") != "local"]
        result: list[dict[str, Any]] = []
        for h in all_hosts:
            paths = _paths(h)
            with self._lock:
                # Local is always connected; remotes use last cached probe result
                # (defaults to False until first probe completes)
                connected = self._connected_cache.get(h["name"], h.get("ssh") is None)
            result.append(
                {
                    "name": h["name"],
                    "connected": connected,
                    "runnableCount": 0,  # filled by get_runnables
                    "groups": [venv_name(p) for p in paths],
                    "role": h.get("role"),
                    "group": h.get("group"),
                }
            )
        return result

    def get_runnables(self, host: str) -> list[dict[str, Any]]:
        with self._lock:
            cache = list(self._runnables_cache)
        if host == "all":
            return cache
        return [r for r in cache if r.get("host") == host]

    # ── history ───────────────────────────────────────────────────────────────

    def get_history(
        self, host: str, runnable: str | None = None
    ) -> list[dict[str, Any]]:
        entry = self._host_entry(host)
        if entry is None:
            return []
        if entry.get("ssh"):
            return self._get_remote_history(entry, runnable)
        paths = _paths(entry)
        records: list[dict[str, Any]] = []
        globs = _log_globs(runnable)
        for rp in paths:
            if not rp:
                continue
            log_dir = Path(rp).parent.parent / "logs"
            if not log_dir.exists():
                # Binary path (e.g. Scripts/runspec.exe) — fall back to ~/logs
                log_dir = Path.home() / "logs"
            if not log_dir.exists():
                continue
            matches = {f for pat in globs for f in log_dir.glob(pat)}
            for log_file in sorted(
                matches, key=lambda p: p.stat().st_mtime, reverse=True
            ):
                records.extend(_parse_log(log_file, host))
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return records[:200]

    def _get_remote_history(
        self, entry: dict[str, Any], runnable: str | None
    ) -> list[dict[str, Any]]:
        from pathlib import PurePosixPath

        ssh = entry["ssh"]
        idf = entry.get("identityFile")
        paths = _paths(entry)
        rp = paths[0] if paths else ""
        log_dir = str(PurePosixPath(rp).parent.parent / "logs")
        script = _remote_log_script(log_dir, runnable)
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

        paths = _paths(entry)
        records: list[dict[str, Any]] = []
        globs = _log_globs(runnable)

        if entry.get("ssh"):
            ssh = entry["ssh"]
            idf = entry.get("identityFile")
            for rp in paths:
                if not rp:
                    continue
                group = PurePosixPath(rp).parent.parent.name
                log_dir = str(PurePosixPath(rp).parent.parent / "logs")
                script = _remote_log_script(log_dir, runnable)
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
        # remote branch, so reusing the name would muddle its type).
        for rp in paths:
            if not rp:
                continue
            group = venv_name(rp)
            local_dir = Path(rp).parent.parent / "logs"
            if not local_dir.exists():
                local_dir = Path.home() / "logs"
            if not local_dir.exists():
                continue
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
        from datetime import datetime, timedelta, timezone

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
        return [(venv_name(rp), rp) for rp in _paths(entry) if rp]

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

    def get_schedules(self) -> list[dict[str, Any]]:
        path = hosts_path().parent / "runspec_schedules.toml"
        if not path.exists():
            return []
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return data.get("schedule", [])

    def create_schedule(self, data: dict[str, Any]) -> None:
        path = hosts_path().parent / "runspec_schedules.toml"
        schedules = self.get_schedules()
        schedules.append(data)
        _write_schedules(path, schedules)

    def delete_schedule(self, id: str) -> None:
        path = hosts_path().parent / "runspec_schedules.toml"
        schedules = [s for s in self.get_schedules() if s.get("id") != id]
        _write_schedules(path, schedules)

    # ── today digest ──────────────────────────────────────────────────────────

    def get_today(self, host: str, group: str) -> dict[str, Any] | None:
        entry = self._host_entry(host)
        if entry is None:
            return None
        paths = _paths(entry)
        rp = next(
            (p for p in paths if venv_name(p) == group), paths[0] if paths else ""
        )
        if not rp:
            return None
        today_file = Path(rp).parent.parent / "runspec_today.json"
        if not today_file.exists():
            return None
        try:
            return json.loads(today_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ── config ────────────────────────────────────────────────────────────────

    def get_config(self) -> dict[str, Any]:
        return read_config()

    def config_dir(self) -> str:
        """Return the resolved config directory path (e.g. C:/Users/jason/AppData/Roaming/runspec-console)."""
        from .config import config_path

        return _normalize_path(str(config_path().parent))

    def save_config(self, data: dict[str, Any]) -> None:
        ssh = data.get("ssh")
        if isinstance(ssh, dict):
            data = {**data, "ssh": _normalize_ssh_section(ssh)}
        write_config(data)
        self._adapter = None
        # SSH tunables (pool, keepalive, max_sessions, max_concurrent, proxy,
        # identity…) may have changed — rebuild the pool against the new config
        # and drop the old transports so the next cycle reflects it.
        old_pool = self._ssh_pool
        self._ssh_pool = self._build_ssh_pool()
        if old_pool is not None:
            old_pool.close_all()

    def _generate_keypair_to_path(self, dest: Path) -> dict[str, Any]:
        """Write a new ed25519 key pair to dest (OpenSSH PEM + PPK v2). Does not touch config."""
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
            from cryptography.hazmat.primitives import serialization as _serial
        except ImportError:
            return {
                "ok": False,
                "public_key": "",
                "key_path": "",
                "message": "pip install cryptography — required for key generation",
            }
        dest.parent.mkdir(parents=True, exist_ok=True)
        private_key = Ed25519PrivateKey.generate()

        dest.write_bytes(
            private_key.private_bytes(
                encoding=_serial.Encoding.PEM,
                format=_serial.PrivateFormat.OpenSSH,
                encryption_algorithm=_serial.NoEncryption(),
            )
        )

        raw_priv = private_key.private_bytes(
            encoding=_serial.Encoding.Raw,
            format=_serial.PrivateFormat.Raw,
            encryption_algorithm=_serial.NoEncryption(),
        )
        raw_pub = private_key.public_key().public_bytes(
            encoding=_serial.Encoding.Raw,
            format=_serial.PublicFormat.Raw,
        )
        _write_ppk_v2(Path(str(dest) + ".ppk"), raw_priv, raw_pub)

        pub = (
            private_key.public_key()
            .public_bytes(
                encoding=_serial.Encoding.OpenSSH,
                format=_serial.PublicFormat.OpenSSH,
            )
            .decode()
            .strip()
        )
        # Persist the public key next to the private key so it can be copied
        # again later (e.g. to authorise it on a host after the fact).
        try:
            Path(str(dest) + ".pub").write_text(pub + "\n", encoding="utf-8")
        except Exception:
            pass
        return {
            "ok": True,
            "public_key": pub,
            "key_path": _normalize_path(str(dest)),
            "message": "",
        }

    def generate_ssh_key(self) -> dict[str, Any]:
        """Generate a fresh ed25519 key pair, backing up any existing key.

        Commits to config immediately — use rotate_ssh_key() when you want the
        safe automated push to remote hosts before committing.
        """
        import os
        import time
        from datetime import datetime, timezone

        key_dir = Path(os.environ.get("APPDATA", "~")).expanduser() / "runspec-console"
        canonical = key_dir / "runspec_ed25519"

        if canonical.exists():
            backup = key_dir / f"runspec_ed25519.bak.{int(time.time())}"
            canonical.rename(backup)

        result = self._generate_keypair_to_path(canonical)
        if not result["ok"]:
            return {**result, "committed": False, "per_host": []}

        cfg = self.get_config()
        ssh_section = dict(cfg.get("ssh", {}))
        ssh_section["identityFile"] = _normalize_path(str(canonical))
        ssh_section["key_created_at"] = datetime.now(timezone.utc).isoformat()
        self.save_config({**cfg, "ssh": ssh_section})

        return {
            "ok": True,
            "committed": True,
            "per_host": [],
            "public_key": result["public_key"],
            "key_path": _normalize_path(str(canonical)),
            "message": f"Key generated at {_normalize_path(str(canonical))}",
        }

    def get_public_key(self) -> dict[str, Any]:
        """Return the current SSH public key, so it can be copied at any time.

        Reads the persisted ``<identityFile>.pub`` if present; otherwise derives
        it from the private key (and writes the ``.pub`` for next time). This is
        what lets the UI show the public key long after generation, even for
        keys created before ``.pub`` files were written.
        """
        cfg = self.get_config()
        idf = (cfg.get("ssh") or {}).get("identityFile", "")
        if not idf:
            return {"ok": False, "public_key": "", "message": "No SSH key configured."}
        priv = Path(idf).expanduser()
        pub_file = Path(str(priv) + ".pub")
        if pub_file.is_file():
            return {
                "ok": True,
                "public_key": pub_file.read_text(encoding="utf-8").strip(),
                "message": "",
            }
        if not priv.is_file():
            return {
                "ok": False,
                "public_key": "",
                "message": f"Key file not found: {idf}",
            }
        try:
            pub = _public_key_from_private(priv)
        except Exception as exc:
            return {
                "ok": False,
                "public_key": "",
                "message": f"Could not read public key: {exc}",
            }
        try:
            pub_file.write_text(pub + "\n", encoding="utf-8")
        except Exception:
            pass
        return {"ok": True, "public_key": pub, "message": ""}

    def rotate_ssh_key(self) -> dict[str, Any]:
        """Safely rotate the SSH key: generate new, push to all remote hosts using the
        old key, verify the new key works, then swap. Active sessions stay alive
        throughout because the old key is not replaced until all hosts are verified."""
        if getattr(self, "_rotating", False):
            return {
                "ok": False,
                "committed": False,
                "public_key": "",
                "key_path": "",
                "per_host": [],
                "message": "Rotation already in progress",
            }
        self._rotating = True
        try:
            return self._do_rotate_ssh_key()
        finally:
            self._rotating = False

    def _do_rotate_ssh_key(self) -> dict[str, Any]:
        import os
        from .executor import ssh_run

        key_dir = Path(os.environ.get("APPDATA", "~")).expanduser() / "runspec-console"
        canonical = key_dir / "runspec_ed25519"
        new_path = key_dir / "runspec_ed25519.new"

        # Step 1 — generate to side path (old key untouched)
        result = self._generate_keypair_to_path(new_path)
        if not result["ok"]:
            return {
                "ok": False,
                "committed": False,
                "public_key": "",
                "key_path": "",
                "per_host": [],
                "message": result["message"],
            }

        public_key = result["public_key"]
        cfg = self.get_config()
        global_ssh = dict(cfg.get("ssh", {}))
        global_idf = global_ssh.get("identityFile", "")

        # Step 2 — collect push targets
        self._reload_hosts()
        remote_hosts = [h for h in self._hosts if h.get("ssh")]

        # Snapshot connection status once (same source as the green dot in the UI)
        # so the slow SSH loop below doesn't hold the lock. Every entry here has an
        # `ssh` target, so a never-probed remote defaults to disconnected (False).
        with self._lock:
            connected_snapshot = dict(self._connected_cache)

        if not remote_hosts:
            self._commit_key_rotation(canonical, new_path, global_ssh, cfg)
            return {
                "ok": True,
                "committed": True,
                "per_host": [],
                "public_key": public_key,
                "key_path": _normalize_path(str(canonical)),
                "message": "Key rotated — no remote hosts configured. Add the public key to each host's authorized_keys before reconnecting.",
            }

        if global_idf and not Path(global_idf).expanduser().exists():
            if new_path.exists():
                new_path.unlink()
            return {
                "ok": False,
                "committed": False,
                "public_key": "",
                "key_path": "",
                "per_host": [],
                "message": f"Current key file not found: {global_idf}. Restore the file or generate a fresh key.",
            }

        # Steps 3 & 4 — push then verify each host
        per_host: list[dict[str, Any]] = []
        for host in remote_hosts:
            name = host.get("name", "?")
            ssh_target = host.get("ssh", "")
            host_idf = host.get("identityFile") or global_idf

            # Skip disconnected hosts — pushing would just fail and block the whole
            # rotation. They keep only the old key and must be updated manually once
            # reconnected (surfaced in per_host + the manual snippet in the UI).
            if not connected_snapshot.get(name, False):
                per_host.append(
                    {
                        "host": name,
                        "pushed": False,
                        "verified": False,
                        "skipped": True,
                        "error": "disconnected — new key not pushed; update manually",
                    }
                )
                continue

            # Skip hosts with a different per-host key — their session uses a different key
            if host.get("identityFile") and host.get("identityFile") != global_idf:
                per_host.append(
                    {
                        "host": name,
                        "pushed": False,
                        "verified": False,
                        "skipped": True,
                        "error": "per-host key override — rotate manually",
                    }
                )
                continue

            # Push new public key using old key
            push_cmd = (
                f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                f"touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
                f"grep -qF '{public_key}' ~/.ssh/authorized_keys || "
                f"printf '%s\\n' '{public_key}' >> ~/.ssh/authorized_keys"
            )
            exit_code, _out, stderr = ssh_run(
                ssh_target,
                push_cmd,
                identity_file=host_idf,
                global_ssh_config=global_ssh,
            )
            if exit_code != 0:
                per_host.append(
                    {
                        "host": name,
                        "pushed": False,
                        "verified": False,
                        "skipped": False,
                        "error": stderr.strip() or f"push failed (exit {exit_code})",
                    }
                )
                continue

            # Verify new key works
            v_code, v_out, v_err = ssh_run(
                ssh_target,
                "echo ok",
                identity_file=str(new_path),
                global_ssh_config=global_ssh,
            )
            verified = v_code == 0 and "ok" in v_out
            per_host.append(
                {
                    "host": name,
                    "pushed": True,
                    "verified": verified,
                    "skipped": False,
                    "error": ""
                    if verified
                    else (
                        v_err.strip()
                        or v_out.strip()
                        or f"verify failed (exit {v_code})"
                    ),
                }
            )

        # Step 5 — commit gate: all non-skipped hosts must be verified, AND at least
        # one host must have actually been verified. The second clause prevents
        # silently committing a key that no reachable host authorizes (every remote
        # skipped because disconnected) — that would lock us out everywhere.
        any_verified = any(h["verified"] for h in per_host)
        can_commit = (
            all(h["verified"] or h["skipped"] for h in per_host) and any_verified
        )

        if not any_verified:
            return {
                "ok": True,
                "committed": False,
                "public_key": public_key,
                "key_path": "",
                "per_host": per_host,
                "message": (
                    "No connected host could be verified — old key kept. "
                    "Connect at least one host and retry, or use Generate new key "
                    "to swap manually."
                ),
            }

        if not can_commit:
            failed = [
                h["host"] for h in per_host if not h["verified"] and not h["skipped"]
            ]
            return {
                "ok": True,
                "committed": False,
                "public_key": public_key,
                "key_path": "",
                "per_host": per_host,
                "message": f"Push or verification failed on: {', '.join(failed)}. Old key still active.",
            }

        # Step 6 — commit
        self._commit_key_rotation(canonical, new_path, global_ssh, cfg)
        verified_n = sum(1 for h in per_host if h["verified"])
        skipped_n = sum(1 for h in per_host if h["skipped"])
        message = f"Key rotated — verified on {verified_n} host(s)."
        if skipped_n:
            message += (
                f" {skipped_n} host(s) skipped (disconnected or per-host key) "
                "— update them manually."
            )
        return {
            "ok": True,
            "committed": True,
            "public_key": public_key,
            "key_path": _normalize_path(str(canonical)),
            "per_host": per_host,
            "message": message,
        }

    def _commit_key_rotation(
        self, canonical: Path, new_path: Path, global_ssh: dict, cfg: dict
    ) -> None:
        import time
        from datetime import datetime, timezone

        ts = int(time.time())
        if canonical.exists():
            canonical.rename(canonical.parent / f"runspec_ed25519.bak.{ts}")
        new_path.rename(canonical)

        # Rotate the PPK alongside the OpenSSH key
        canonical_ppk = Path(str(canonical) + ".ppk")
        new_ppk = Path(str(new_path) + ".ppk")
        if canonical_ppk.exists():
            canonical_ppk.rename(canonical.parent / f"runspec_ed25519.ppk.bak.{ts}")
        if new_ppk.exists():
            new_ppk.rename(canonical_ppk)

        # Rotate the .pub alongside too
        canonical_pub = Path(str(canonical) + ".pub")
        new_pub = Path(str(new_path) + ".pub")
        if canonical_pub.exists():
            canonical_pub.rename(canonical.parent / f"runspec_ed25519.pub.bak.{ts}")
        if new_pub.exists():
            new_pub.rename(canonical_pub)

        ssh_section = dict(global_ssh)
        ssh_section["identityFile"] = _normalize_path(str(canonical))
        ssh_section["key_created_at"] = datetime.now(timezone.utc).isoformat()
        self.save_config({**cfg, "ssh": ssh_section})

    # ── PuTTY integration ─────────────────────────────────────────────────────

    def puttygen_path(self) -> str:
        """Locate puttygen.exe in common PuTTY install locations or on PATH."""
        found = _find_putty_exe("puttygen.exe")
        return str(found) if found else ""

    def launch_puttygen(self) -> None:
        """Launch puttygen.exe in the background (fire and forget)."""
        import subprocess as _sp

        path = self.puttygen_path()
        if not path:
            return
        try:
            _sp.Popen([path], close_fds=True)
        except Exception:
            pass

    def open_putty_url(self, url: str) -> None:
        """Open a URL in the user's default browser."""
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def browse_ssh_binary(self) -> str:
        """Open a native file picker for an SSH binary; returns the chosen path or ''.

        The path is normalised to forward slashes before returning so the UI
        input field and config.toml see a consistent representation.
        """
        import webview

        windows = getattr(webview, "windows", None) or []
        if not windows:
            return ""
        try:
            result = windows[0].create_file_dialog(
                webview.FileDialog.OPEN,
                file_types=("Executables (*.exe)",),
            )
        except Exception:
            return ""
        if not result:
            return ""
        if isinstance(result, (list, tuple)):
            chosen = str(result[0]) if result else ""
        else:
            chosen = str(result)
        return _normalize_path(chosen)

    # ── hosts file management ─────────────────────────────────────────────────

    def test_host(self, name: str) -> dict[str, Any]:
        entry = self._host_entry(name)
        if entry is None:
            return {
                "connected": False,
                "runspec_ok": False,
                "runnable_count": 0,
                "stdout": "",
                "stderr": f"Host '{name}' not found in hosts file",
                "exit_code": -1,
            }

        ssh = entry.get("ssh")
        paths = _paths(entry)
        rp = paths[0] if paths else ""
        idf = entry.get("identityFile")

        if ssh:
            code, out, err = self._ssh_run(
                ssh, f"{rp} local --format json", identity_file=idf
            )
            connected = code != -1  # -1 means connection itself failed
            ok = code == 0
            count = 0
            if ok:
                try:
                    import json as _json

                    count = len(_json.loads(out))
                except Exception:
                    ok = False
            return {
                "connected": connected,
                "runspec_ok": ok,
                "runnable_count": count,
                "stdout": out[:2000],
                "stderr": err[:2000],
                "exit_code": code,
            }
        else:
            import subprocess as _sp

            try:
                r = _sp.run(
                    [rp, "local", "--format", "json"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception as exc:
                return {
                    "connected": False,
                    "runspec_ok": False,
                    "runnable_count": 0,
                    "stdout": "",
                    "stderr": str(exc),
                    "exit_code": -1,
                }
            ok = r.returncode == 0
            count = 0
            if ok:
                try:
                    import json as _json

                    count = len(_json.loads(r.stdout))
                except Exception:
                    ok = False
            return {
                "connected": True,
                "runspec_ok": ok,
                "runnable_count": count,
                "stdout": r.stdout[:2000],
                "stderr": r.stderr[:2000],
                "exit_code": r.returncode,
            }

    def get_jump_hosts(self) -> list[dict[str, Any]]:
        self._reload_hosts()
        result: list[dict[str, Any]] = []
        for h in self._hosts:
            ssh = h.get("ssh", "")
            user, hostname, port = _parse_ssh(ssh)
            entry: dict[str, Any] = {
                "name": h["name"],
                "hostname": hostname,
                "runspec_paths": _paths(h),
            }
            if user:
                entry["user"] = user
            if port is not None:
                entry["port"] = port
            if h.get("identityFile"):
                entry["identityFile"] = h["identityFile"]
            if h.get("group"):
                entry["group"] = h["group"]
            result.append(entry)
        return result

    def save_jump_hosts(self, hosts: list[dict[str, Any]]) -> None:
        entries: list[dict[str, Any]] = []
        for h in hosts:
            hostname = h.get("hostname", "")
            user = h.get("user", "")
            port = h.get("port")
            ssh = f"{user}@{hostname}" if user else hostname
            if port:
                ssh = f"{ssh}:{port}"
            raw_paths = h.get("runspec_paths") or []
            if not raw_paths and h.get("runspec_path"):
                raw_paths = [h["runspec_path"]]
            # Normalise filesystem paths to forward slashes for TOML storage.
            # Remote runspec paths are POSIX so this is a no-op for them;
            # identityFile on Windows benefits from the conversion.
            normalised_paths = [_normalize_path(p) for p in raw_paths]
            entry: dict[str, Any] = {
                "name": h["name"],
                "ssh": ssh,
                "runspec_paths": normalised_paths or ["runspec"],
            }
            if h.get("identityFile"):
                entry["identityFile"] = _normalize_path(h["identityFile"])
            if h.get("group"):
                entry["group"] = h["group"]
            entries.append(entry)
        save_hosts(hosts_path(), entries)
        self._reload_hosts()
        threading.Thread(target=self._refresh_cycle, daemon=True).start()

    def import_jump_hosts(self, toml_content: str) -> list[dict[str, Any]]:
        try:
            data = tomllib.loads(toml_content)
        except Exception:
            return []
        imported = data.get("host", [])
        existing = {h["name"] for h in self._hosts}
        new = [h for h in imported if h.get("name") and h["name"] not in existing]
        self._hosts.extend(new)
        save_hosts(hosts_path(), self._hosts)
        threading.Thread(target=self._refresh_cycle, daemon=True).start()
        return new

    # ── invocation ────────────────────────────────────────────────────────────

    def _become_params(
        self, cached: dict[str, Any] | None, hostname: str
    ) -> tuple[str, str, str | None]:
        """Resolve run_as + become method/flags for a runnable on a host.

        Reads the runnable's raw spec (carried through discovery as ``rawSpec``)
        and resolves ``run_as`` for this host. Returns ("", ...) when no
        privilege escalation is configured."""
        from runspec.become import resolve_run_as

        raw = (cached or {}).get("rawSpec") or {}
        run_as = resolve_run_as(raw.get("run_as"), hostname)
        return run_as, str(raw.get("become_method") or "sudo"), raw.get("become_flags")

    def invoke_runnable(
        self,
        host: str,
        runnable: str,
        args: dict[str, Any],
        command_path: list[str] | None = None,
        group: str | None = None,
    ) -> str:
        inv_id = uuid.uuid4().hex[:12]
        cp = command_path or []
        entry = self._host_entry(host)
        cancel_event = threading.Event()

        entry_paths = _paths(entry) if entry else []
        # Look up the runnable's discovery record — needed for its venv (group)
        # and its run_as / become_method (privilege escalation).
        with self._lock:
            cached = next(
                (
                    r
                    for r in self._runnables_cache
                    if r.get("host") == host and r.get("name") == runnable
                ),
                None,
            )
        if group is None and cached:
            group = cached.get("group")
        rp = next(
            (p for p in entry_paths if venv_name(p) == group),
            entry_paths[0] if entry_paths else "",
        )

        run_as, become_method, become_flags = self._become_params(cached, host)

        with self._lock:
            self._in_flight[inv_id] = {
                "id": inv_id,
                "runnable": runnable,
                "group": venv_name(rp) if rp else "",
                "host": host,
                "operator": self._current_user(),
                "runAs": run_as,
                "startedAt": _iso_now(),
                "args": args,
                _CANCEL_KEY: cancel_event,
            }

        def run() -> None:
            def on_line(line: str, stream: str) -> None:
                self._dispatch(
                    "runspec:output", {"id": inv_id, "line": line, "stream": stream}
                )

            def on_done(exit_code: int, duration_ms: int) -> None:
                with self._lock:
                    self._in_flight.pop(inv_id, None)
                self._dispatch(
                    "runspec:run_end",
                    {"id": inv_id, "exit_code": exit_code, "duration_ms": duration_ms},
                )

            if entry is None or not rp:
                on_line(f"✗  Host '{host}' not found in runspec_hosts.toml", "stderr")
                on_done(-1, 0)
                return

            ssh = entry.get("ssh")
            if ssh:
                # Stream over a pooled transport when pooling is on — the
                # invocation reuses the host's warm connection (one more channel,
                # no new handshake) instead of opening a fresh client.
                pool = self._ssh_pool
                session_factory = None
                if pool is not None:
                    cfg = self.get_config().get("ssh")
                    idf = entry.get("identityFile")

                    def session_factory() -> tuple[Any, Any]:
                        return pool.open_session(
                            ssh, identity_file=idf, global_ssh_config=cfg
                        )

                run_remote(
                    ssh,
                    rp,
                    runnable,
                    args,
                    cp,
                    on_line,
                    on_done,
                    entry.get("identityFile"),
                    cancel_event=cancel_event,
                    global_ssh_config=self.get_config().get("ssh"),
                    run_as=run_as,
                    become_method=become_method,
                    become_flags=become_flags,
                    session_factory=session_factory,
                )
            else:
                run_local(
                    rp,
                    runnable,
                    args,
                    cp,
                    on_line,
                    on_done,
                    cancel_event=cancel_event,
                    run_as=run_as,
                    become_method=become_method,
                    become_flags=become_flags,
                )

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return inv_id

    def get_in_flight(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {k: v for k, v in inv.items() if k != _CANCEL_KEY}
                for inv in self._in_flight.values()
            ]

    def cancel_invocation(self, inv_id: str) -> None:
        """Signal a running invocation to stop. Best-effort: kill the subprocess."""
        with self._lock:
            inv = self._in_flight.get(inv_id)
        if inv is None:
            return
        ev: threading.Event | None = inv.get(_CANCEL_KEY)
        if ev is not None:
            ev.set()

    # ── chat ──────────────────────────────────────────────────────────────────

    def send_chat(self, message: str, invocation_id: str | None = None) -> str:
        chat_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._chat_cancels[chat_id] = threading.Event()

        def run() -> None:
            try:
                try:
                    adapter = self._get_adapter()
                except Exception as exc:
                    self._dispatch(
                        "runspec:token", {"id": chat_id, "token": f"⚠ {exc}"}
                    )
                    self._dispatch(
                        "runspec:run_end",
                        {"id": chat_id, "exit_code": 1, "duration_ms": 0},
                    )
                    return
                if adapter is None:
                    self._dispatch(
                        "runspec:token",
                        {
                            "id": chat_id,
                            "token": "⚠ No LLM provider configured. Set provider and API key in Settings.",
                        },
                    )
                    self._dispatch(
                        "runspec:run_end",
                        {"id": chat_id, "exit_code": 1, "duration_ms": 0},
                    )
                    return
                asyncio.run(self._agentic_chat_turn(chat_id, message, adapter))
            finally:
                with self._lock:
                    self._chat_cancels.pop(chat_id, None)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return chat_id

    def cancel_chat(self, chat_id: str) -> None:
        """Signal an in-flight chat turn to stop. Best-effort: it halts at the
        next safe checkpoint — no further model calls and no further tool runs.
        A tool already executing (e.g. a remote command) is left to finish."""
        with self._lock:
            event = self._chat_cancels.get(chat_id)
        if event is not None:
            logger.info("chat %s: cancel requested", chat_id)
            event.set()
        # Wake any confirmation prompt waiting on this turn — otherwise Stop does
        # nothing while the Approve/Deny dialog is up (the gate is parked in
        # event.wait, which the cancel flag alone never interrupts). Mark them
        # cancelled so the gate refuses the tool rather than running it.
        with self._lock:
            pending = [
                (confirm_event, holder)
                for cid, confirm_event, holder in self._pending_confirms.values()
                if cid == chat_id
            ]
        for confirm_event, holder in pending:
            holder["cancelled"] = True
            confirm_event.set()

    def clear_chat(self) -> None:
        """Start a fresh conversation — drop the model's memory of prior turns.

        Blocks on the conversation lock so it can't run mid-turn (a turn in
        flight finishes against its history, then the next turn starts empty)."""
        with self._chat_lock:
            self._chat_history = []

    def _trim_chat_history(self, max_messages: int = 60) -> None:
        """Bound the rolling conversation so a long-lived session doesn't grow
        context without limit. Trims oldest messages, then drops any leading
        assistant / tool-result turn so the kept history still starts on a plain
        user message — the API requires that, and it keeps tool_use/tool_result
        pairs intact. Caller must hold self._chat_lock."""
        hist = self._chat_history
        if len(hist) <= max_messages:
            return
        del hist[: len(hist) - max_messages]
        while hist and not (
            hist[0].get("role") == "user" and isinstance(hist[0].get("content"), str)
        ):
            hist.pop(0)

    async def _agentic_chat_turn(
        self, chat_id: str, message: str, adapter: Any
    ) -> None:
        start = time.monotonic()
        tools = self._runnables_to_tools()
        usage_total = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        cancel = self._chat_cancels.get(chat_id) or threading.Event()
        stopped = False

        # Hold the conversation lock for the whole turn: it both serialises
        # concurrent sends and is the single owner of self._chat_history.
        with self._chat_lock:
            history = self._chat_history
            history.append({"role": "user", "content": message})

            for _ in range(10):  # max agentic iterations
                if cancel.is_set():
                    stopped = True
                    break
                response: Any = None
                assistant_text = ""  # this iteration's streamed assistant text
                try:
                    async for event in adapter.stream_with_tools(history, tools):
                        if cancel.is_set():
                            stopped = True
                            break  # abort streaming (closes the provider stream)
                        if event[0] == "text":
                            assistant_text += event[1]
                            self._dispatch(
                                "runspec:token", {"id": chat_id, "token": event[1]}
                            )
                        elif event[0] == "done":
                            response = event[1]
                except Exception as exc:
                    self._dispatch(
                        "runspec:token", {"id": chat_id, "token": f"\n⚠ Error: {exc}"}
                    )
                    break

                if response is not None:
                    for key, value in self._usage_from_response(response).items():
                        usage_total[key] += value

                # Persisting any clean assistant text is always safe (text-only,
                # no tool pairing); do it whenever we're about to leave the loop.
                if stopped:
                    if assistant_text.strip():
                        history.append({"role": "assistant", "content": assistant_text})
                    break

                if (
                    response is None
                    or response.stop_reason != "tool_use"
                    or not response.tool_calls
                ):
                    # Final turn — persist the assistant's reply so the next user
                    # message sees it (tool-calling turns are persisted below via
                    # make_tool_turn, which already carries the assistant turn).
                    if response is not None and assistant_text.strip():
                        history.append({"role": "assistant", "content": assistant_text})
                    break

                # About to act on hosts — honour a stop requested while the model
                # was producing the tool calls, before running any of them.
                if cancel.is_set():
                    stopped = True
                    if assistant_text.strip():
                        history.append({"role": "assistant", "content": assistant_text})
                    break

                # Execute each tool call and dispatch events
                tool_results: list[tuple[Any, str]] = []
                for tc in response.tool_calls:
                    self._dispatch(
                        "runspec:tool_start",
                        {
                            "id": chat_id,
                            "tool_name": tc.name,
                            "tool_input": tc.input,
                        },
                    )
                    if cancel.is_set():
                        # Stopped mid-batch — refuse the rest without prompting
                        # or running, but still record a paired tool_result.
                        output = f"⏹ '{tc.name.split('__', 1)[-1]}' was not run — you stopped the assistant."
                    else:
                        output = await self._gated_run_tool_async(
                            tc.name, tc.input, chat_id
                        )
                    self._dispatch(
                        "runspec:tool_end",
                        {
                            "id": chat_id,
                            "tool_name": tc.name,
                            "output": output[:2000],
                        },
                    )
                    tool_results.append((tc, output))

                history.extend(adapter.make_tool_turn(response, tool_results))

            self._trim_chat_history()

        duration_ms = int((time.monotonic() - start) * 1000)
        if stopped:
            self._dispatch("runspec:token", {"id": chat_id, "token": "\n⏹ Stopped."})
        self._dispatch(
            "runspec:run_end",
            {"id": chat_id, "exit_code": 0, "duration_ms": duration_ms},
        )
        if any(usage_total.values()):
            self._dispatch(
                "runspec:chat_usage",
                {
                    "id": chat_id,
                    # input_tokens is the uncached remainder (full price); the cached
                    # portion is reported separately so the UI can show cache savings.
                    "input_tokens": usage_total["input"],
                    "output_tokens": usage_total["output"],
                    "cache_read_tokens": usage_total["cache_read"],
                    "cache_creation_tokens": usage_total["cache_creation"],
                },
            )

    @staticmethod
    def _usage_from_response(response: Any) -> dict[str, int]:
        """Pull token counts off a provider response. Anthropic/Bedrock split the
        prompt into uncached `input_tokens` plus `cache_read_input_tokens` (served
        at ~0.1x) and `cache_creation_input_tokens` (the first-turn write); OpenAI
        reports only prompt/completion totals (cache fields resolve to 0)."""
        zero = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        usage = getattr(getattr(response, "_raw", None), "usage", None)
        if usage is None:
            return zero

        def field(*names: str) -> int:
            for name in names:
                value = getattr(usage, name, None)
                if value:
                    return int(value)
            return 0

        return {
            "input": field("input_tokens", "prompt_tokens"),
            "output": field("output_tokens", "completion_tokens"),
            "cache_read": field("cache_read_input_tokens"),
            "cache_creation": field("cache_creation_input_tokens"),
        }

    def _runnables_to_tools(self) -> list[dict[str, Any]]:
        """Convert cached runnables to Anthropic-format tool schemas."""
        import re

        with self._lock:
            runnables = list(self._runnables_cache)
        tools: list[dict[str, Any]] = []
        seen_tool_names: set[str] = set()
        for r in runnables:
            host = r.get("host", "local")
            name = r.get("name", "")
            raw_name = f"{host}__{name}"
            tool_name = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_name)[:64]
            if tool_name in seen_tool_names:
                sys.stderr.write(
                    f"runspec-console: warning: duplicate tool name '{tool_name}' skipped"
                    f" ('{name}' defined in multiple installed packages)\n"
                )
                sys.stderr.flush()
                continue
            seen_tool_names.add(tool_name)
            description = r.get("description") or f"Run {name} on {host}"
            args = r.get("args") or []
            properties: dict[str, Any] = {}
            required: list[str] = []
            for arg in args:
                arg_name = arg.get("name", "")
                if not arg_name:
                    continue
                arg_type = arg.get("type", "str")
                json_type = {
                    "int": "integer",
                    "float": "number",
                    "flag": "boolean",
                    "bool": "boolean",
                    "path": "string",
                }.get(arg_type, "string")
                prop: dict[str, Any] = {"type": json_type}
                # Build description: user-facing text + default hint so the LLM
                # knows what value will be used when the arg is omitted.
                desc_parts: list[str] = []
                if arg.get("description") or arg.get("help"):
                    desc_parts.append(str(arg.get("description") or arg.get("help")))
                default = arg.get("default")
                if default is not None:
                    if isinstance(default, str) and default.startswith("$"):
                        desc_parts.append(f"(default from env var {default})")
                    else:
                        desc_parts.append(f"(default: {default})")
                if desc_parts:
                    prop["description"] = " ".join(desc_parts)
                if arg.get("options"):
                    prop["enum"] = arg["options"]
                properties[arg_name] = prop
                if arg.get("required"):
                    required.append(arg_name)
            schema: dict[str, Any] = {"type": "object", "properties": properties}
            if required:
                schema["required"] = required
            tools.append(
                {"name": tool_name, "description": description, "input_schema": schema}
            )
        return tools

    def _effective_tool_autonomy(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> str:
        """Autonomy the agent must honour for this call: the runnable's level,
        escalated by any provided arg's per-arg autonomy (most restrictive wins).
        Unknown runnable → 'confirm' (gated by default — never silently run)."""
        host, runnable = (
            tool_name.split("__", 1) if "__" in tool_name else (None, tool_name)
        )
        with self._lock:
            r = next(
                (
                    r
                    for r in self._runnables_cache
                    if r.get("host") == host and r.get("name") == runnable
                ),
                None,
            )
        if not r:
            return "confirm"
        level = r.get("autonomy") or "confirm"
        for arg in r.get("args") or []:
            if arg.get("name") in (tool_input or {}) and arg.get("autonomy"):
                if _AUTONOMY_RANK.get(arg["autonomy"], 0) > _AUTONOMY_RANK.get(
                    level, 0
                ):
                    level = arg["autonomy"]
        return level

    # Confirmation timeout — no answer from the UI within this window is a deny.
    _CONFIRM_TIMEOUT_S = 300

    def _confirm_decision(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Resolve the gate decision string: 'run' | 'manual' | <level needing prompt>."""
        level = self._effective_tool_autonomy(tool_name, tool_input)
        if level == "autonomous":
            return "run"
        if level == "manual":
            return "manual"
        return level  # confirm / supervised → needs a prompt

    def _register_confirm(
        self, chat_id: str, tool_name: str, tool_input: dict[str, Any], level: str
    ) -> tuple[str, threading.Event, dict[str, bool]]:
        """Register a pending confirmation and emit the UI prompt event.

        Returns (request_id, event, holder). The event is set by
        resolve_tool_confirmation() when the UI answers."""
        request_id = uuid.uuid4().hex
        event = threading.Event()
        holder = {"approved": False, "cancelled": False}
        with self._lock:
            self._pending_confirms[request_id] = (chat_id, event, holder)
        logger.info(
            "autonomy gate: prompting for %s (autonomy=%s) request_id=%s",
            tool_name,
            level,
            request_id,
        )
        self._dispatch(
            "runspec:tool_confirm",
            {
                "id": chat_id,
                "request_id": request_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "autonomy": level,
            },
        )
        return request_id, event, holder

    def _finish_confirm(
        self, request_id: str, holder: dict[str, bool], timed_out: bool
    ) -> bool:
        with self._lock:
            self._pending_confirms.pop(request_id, None)
        if timed_out:
            logger.warning(
                "autonomy gate: request_id=%s timed out after %ds — treating as deny",
                request_id,
                self._CONFIRM_TIMEOUT_S,
            )
        else:
            logger.info(
                "autonomy gate: request_id=%s answered approved=%s",
                request_id,
                holder["approved"],
            )
        return holder["approved"]

    async def _gated_run_tool_async(
        self, tool_name: str, tool_input: dict[str, Any], chat_id: str
    ) -> str:
        """Async autonomy gate used by the agentic loop.

        The UI prompt is dispatched from the event-loop thread (the same thread
        that streams tokens, which is known to reach the frontend) — only the
        blocking wait is offloaded to a worker thread. Dispatching the prompt
        from a worker thread was unreliable and produced silent timeouts."""
        decision = self._confirm_decision(tool_name, tool_input)
        display = tool_name.split("__", 1)[-1]
        logger.info("autonomy gate: tool=%s decision=%s", tool_name, decision)
        if decision == "manual":
            return f"✗ '{display}' is manual-only (autonomy = manual) and cannot be run by the assistant. Ask the user to run it themselves from the Run button."
        if decision != "run":
            request_id, event, holder = self._register_confirm(
                chat_id, tool_name, tool_input, decision
            )
            timed_out = not await asyncio.to_thread(event.wait, self._CONFIRM_TIMEOUT_S)
            approved = self._finish_confirm(request_id, holder, timed_out)
            # Stop pressed while the dialog was up: don't run, and let the loop
            # halt at its next checkpoint (cancel flag is already set).
            if holder.get("cancelled"):
                return f"⏹ '{display}' was not run — you stopped the assistant before approving it."
            if not approved:
                return (
                    f"✗ The user declined to run '{display}' (autonomy = {decision})."
                )
        return await asyncio.to_thread(self._run_tool_sync, tool_name, tool_input)

    def _gated_run_tool(
        self, tool_name: str, tool_input: dict[str, Any], chat_id: str
    ) -> str:
        """Synchronous autonomy gate (kept for direct/unit-test use).

        autonomous → run; confirm/supervised → ask the user (Approve/Deny);
        manual → refuse (human-only; the user must use the Run button)."""
        decision = self._confirm_decision(tool_name, tool_input)
        display = tool_name.split("__", 1)[-1]
        if decision == "manual":
            return f"✗ '{display}' is manual-only (autonomy = manual) and cannot be run by the assistant. Ask the user to run it themselves from the Run button."
        if decision != "run":
            if not self._await_confirmation(tool_name, tool_input, decision, chat_id):
                return (
                    f"✗ The user declined to run '{display}' (autonomy = {decision})."
                )
        return self._run_tool_sync(tool_name, tool_input)

    def _await_confirmation(
        self, tool_name: str, tool_input: dict[str, Any], level: str, chat_id: str
    ) -> bool:
        """Ask the UI to approve an agent tool call; block (in a worker thread)
        until resolve_tool_confirmation() fires. Times out as a deny."""
        request_id, event, holder = self._register_confirm(
            chat_id, tool_name, tool_input, level
        )
        timed_out = not event.wait(timeout=self._CONFIRM_TIMEOUT_S)
        return self._finish_confirm(request_id, holder, timed_out)

    def resolve_tool_confirmation(self, request_id: str, approved: bool) -> None:
        """Frontend callback: resolve a pending agent tool-call confirmation."""
        logger.info(
            "autonomy gate: UI resolved request_id=%s approved=%s", request_id, approved
        )
        with self._lock:
            entry = self._pending_confirms.get(request_id)
        if entry is not None:
            _chat_id, event, holder = entry
            holder["approved"] = bool(approved)
            event.set()
        else:
            logger.warning(
                "autonomy gate: resolve for unknown request_id=%s (already timed out?)",
                request_id,
            )

    def _run_tool_sync(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Blocking runnable execution — call via asyncio.to_thread from the agentic loop."""
        if "__" not in tool_name:
            return f"Error: invalid tool name '{tool_name}'"
        host, runnable = tool_name.split("__", 1)
        entry = self._host_entry(host)
        if entry is None:
            return f"Error: host '{host}' not found"
        output_lines: list[str] = []
        exit_code_holder = [0]

        def on_line(line: str, stream: str) -> None:
            output_lines.append(line)

        def on_done(exit_code: int, duration_ms: int) -> None:
            exit_code_holder[0] = exit_code

        tool_paths = _paths(entry)
        with self._lock:
            cached_r = next(
                (
                    r
                    for r in self._runnables_cache
                    if r.get("host") == host and r.get("name") == runnable
                ),
                None,
            )
        run_group = cached_r.get("group") if cached_r else None
        rp = next(
            (p for p in tool_paths if venv_name(p) == run_group),
            tool_paths[0] if tool_paths else "",
        )
        run_as, become_method, become_flags = self._become_params(cached_r, host)
        ssh = entry.get("ssh")
        if ssh:
            run_remote(
                ssh,
                rp,
                runnable,
                tool_input,
                [],
                on_line,
                on_done,
                entry.get("identityFile"),
                timeout=120,
                agent=True,
                global_ssh_config=self.get_config().get("ssh"),
                run_as=run_as,
                become_method=become_method,
                become_flags=become_flags,
            )
        else:
            run_local(
                rp,
                runnable,
                tool_input,
                [],
                on_line,
                on_done,
                timeout=120,
                agent=True,
                run_as=run_as,
                become_method=become_method,
                become_flags=become_flags,
            )

        output = "\n".join(output_lines)
        if len(output) > 16384:
            output = output[:16384] + "\n[output truncated]"
        if exit_code_holder[0] != 0:
            return f"[exit {exit_code_holder[0]}]\n{output}"
        return output or "(no output)"

    # ── terminal sessions ─────────────────────────────────────────────────────

    def launch_terminal(self, host: str) -> None:
        """Launch PuTTY in a separate window connected to the host.

        Fire-and-forget: no stdout/stderr capture, no session tracking. PuTTY
        runs as its own top-level process and the user closes it from there.
        """
        import subprocess as _sp

        entry = self._host_entry(host)
        if entry is None:
            raise ValueError(f"Host '{host}' not found")
        ssh = entry.get("ssh")
        if not ssh:
            raise ValueError(
                "Cannot launch terminal to local host — use a remote jump host"
            )
        putty = _find_putty_exe("putty.exe")
        if putty is None:
            raise ValueError(
                r"putty.exe not found. Install PuTTY from putty.org — "
                r"expected at C:\Program Files\PuTTY\putty.exe."
            )
        # Parse ssh target for hostname/port; get username from host or global config.
        cfg_ssh = self.get_config().get("ssh", {})
        user, hostname, port = _parse_ssh(ssh)
        if not user:
            user = cfg_ssh.get("user", "")

        cmd = [str(putty)]
        # Route through the HTTP proxy by loading a saved session that carries
        # the proxy config (PuTTY has no proxy CLI flag). Host/user/key below
        # still override the session.
        proxy = cfg_ssh.get("proxy") or ""
        if proxy:
            session = _ensure_putty_proxy_session(proxy)
            if session:
                cmd.extend(["-load", session])
        cmd.append("-ssh")
        if user:
            cmd.extend(["-l", user])
        cmd.append(hostname)
        if port:
            cmd.extend(["-P", str(port)])

        # Prefer PPK v2 (works with all PuTTY versions) over raw OpenSSH key.
        idf = cfg_ssh.get("identityFile", "")
        if idf:
            expanded = Path(idf).expanduser()
            ppk = Path(str(expanded) + ".ppk")
            key_file = ppk if ppk.exists() else expanded
            if not key_file.exists():
                raise ValueError(
                    f"SSH key file not found: {idf}\n"
                    "Generate a new key in Settings → SSH."
                )
            cmd.extend(["-i", str(key_file)])
        _sp.Popen(cmd)

    def launch_local_terminal(self) -> None:
        """Open a local terminal window — tries Windows Terminal, then PowerShell, then cmd."""
        import subprocess as _sp

        for exe in ("wt.exe", "powershell.exe", "cmd.exe"):
            try:
                _sp.Popen([exe])
                return
            except FileNotFoundError:
                continue
        raise ValueError("Could not open a terminal window")

    # ── internals ─────────────────────────────────────────────────────────────

    def _reload_hosts(self) -> None:
        self._hosts = load_hosts(hosts_path())

    def _local_host_entry(self) -> dict[str, Any]:
        """Synthetic entry for the venv runspec-console itself runs in."""
        scripts = Path(sys.executable).parent
        # Windows venvs use Scripts\runspec.exe; Linux/Mac use bin/runspec
        for name in ("runspec.exe", "runspec"):
            candidate = scripts / name
            if candidate.exists():
                return {"name": "local", "runspec_paths": [str(candidate)], "ssh": None}
        # Fallback: construct the expected path even if not yet installed
        return {
            "name": "local",
            "runspec_paths": [str(scripts / "runspec.exe")],
            "ssh": None,
        }

    def _host_entry(self, host: str) -> dict[str, Any] | None:
        if host == "local":
            return self._local_host_entry()
        return next((h for h in self._hosts if h.get("name") == host), None)

    def _start_refresh_watcher(self) -> None:
        """Run a full refresh cycle, then repeat every ``[refresh] interval`` s."""
        from .ssh_pool import refresh_settings

        def _loop() -> None:
            while True:
                self._refresh_cycle()
                interval = refresh_settings(self.get_config().get("refresh"))[
                    "interval"
                ]
                time.sleep(interval)

        threading.Thread(target=_loop, daemon=True).start()

    def _refresh_cycle(self) -> None:
        """Probe connectivity, then discover runnables — reusing pooled connections.

        With the pool on, Phase 1 *warms* the per-host transport that Phase 2's
        discovery then reuses, so a host costs one handshake per cycle (often
        zero once warm) instead of the old ``probe + per-path`` handshake storm.
        Per-host jitter spreads the cycle's connects so they don't all fire on
        the same instant; the pool's global semaphore bounds how many connect at
        once; ``reap()`` closes transports gone idle.
        """
        from .ssh_pool import refresh_settings

        self._reload_hosts()
        local = self._local_host_entry()
        all_hosts = [local] + [h for h in self._hosts if h.get("name") != "local"]
        jitter = refresh_settings(self.get_config().get("refresh"))["jitter"]

        # ── Phase 1: connectivity (warms the pooled transport) ─────────────────
        with self._lock:
            self._connected_cache["local"] = True

        def probe(h: dict[str, Any]) -> None:
            if jitter:
                time.sleep(random.uniform(0, jitter))  # spread the connects
            result = self._check_connected(h)
            with self._lock:
                self._connected_cache[h["name"]] = result

        probe_threads = [
            threading.Thread(target=probe, args=(h,), daemon=True)
            for h in all_hosts
            if h.get("ssh")
        ]
        for t in probe_threads:
            t.start()
        for t in probe_threads:
            t.join()
        self._dispatch("runspec:hosts_updated", {})

        # ── Phase 2: runnables discovery (reuses the warm transport) ───────────
        discovered: list[dict[str, Any]] = []
        lock2 = threading.Lock()
        runner = self._ssh_pool.run if self._ssh_pool is not None else None

        def discover(h: dict[str, Any]) -> None:
            paths = _paths(h)
            ssh = h.get("ssh")
            idf = h.get("identityFile")
            name = h["name"]
            with self._lock:
                connected = self._connected_cache.get(name, ssh is None)
            if not connected:
                return
            for rp in paths:
                items = (
                    discover_remote(
                        ssh,
                        rp,
                        name,
                        idf,
                        global_ssh_config=self.get_config().get("ssh"),
                        runner=runner,
                    )
                    if ssh
                    else discover_local(rp, name)
                )
                with lock2:
                    discovered.extend(items)

        disc_threads = [
            threading.Thread(target=discover, args=(h,), daemon=True) for h in all_hosts
        ]
        for t in disc_threads:
            t.start()
        for t in disc_threads:
            t.join()

        seen_keys: set[tuple[str, str]] = set()
        deduped: list[dict[str, Any]] = []
        for r in discovered:
            key = (r.get("host", ""), r.get("name", ""))
            if key in seen_keys:
                sys.stderr.write(
                    f"runspec-console: warning: '{key[1]}' defined in multiple"
                    f" installed packages on host '{key[0]}', keeping first\n"
                )
                sys.stderr.flush()
            else:
                seen_keys.add(key)
                deduped.append(r)
        with self._lock:
            self._runnables_cache = deduped
        self._dispatch("runspec:runnables_updated", {})

        # Close transports that have gone idle beyond their TTL (cheap, per-cycle
        # — no separate timer thread).
        if self._ssh_pool is not None:
            self._ssh_pool.reap()

    def _check_connected(self, host: dict[str, Any]) -> bool:
        ssh = host.get("ssh")
        if not ssh:
            return True
        pool = self._ssh_pool
        if pool is not None:
            # Probe-free: obtaining a live pooled transport IS the connectivity
            # check, and it warms the connection discovery reuses this cycle —
            # no separate ``ssh host true`` handshake.
            return pool.connectable(
                ssh,
                identity_file=host.get("identityFile"),
                global_ssh_config=self.get_config().get("ssh"),
            )
        from .executor import ssh_run

        code, _, _ = ssh_run(
            ssh,
            "true",
            identity_file=host.get("identityFile"),
            global_ssh_config=self.get_config().get("ssh"),
            timeout=5,
        )
        return code == 0

    def _get_adapter(self) -> Any:
        if self._adapter is not None:
            return self._adapter
        cfg = self.get_config()
        provider = cfg.get("llm", {}).get("provider")
        if not provider:
            return None
        kwargs: dict[str, Any] = {}
        llm_cfg = cfg.get("llm", {})
        if llm_cfg.get("api_key"):
            kwargs["api_key"] = llm_cfg["api_key"]
        if llm_cfg.get("model"):
            kwargs["model"] = llm_cfg["model"]
        # Optional standing instructions for the assistant (site policy, host
        # context, tone). Only override when set so adapters keep their default.
        if str(llm_cfg.get("system", "")).strip():
            kwargs["system"] = llm_cfg["system"]
        if llm_cfg.get("aws_region"):
            kwargs["aws_region"] = llm_cfg["aws_region"]
        if llm_cfg.get("base_url"):
            kwargs["base_url"] = llm_cfg["base_url"]
        if llm_cfg.get("api_key_command"):
            kwargs["api_key_command"] = llm_cfg["api_key_command"]
        if llm_cfg.get("api_key_ttl_ms") is not None:
            kwargs["api_key_ttl_ms"] = int(llm_cfg["api_key_ttl_ms"])
        try:
            from .adapters.base import load_adapter

            adapter = load_adapter(provider, **kwargs)
        except ImportError:
            raise ImportError(
                f"LLM provider '{provider}' requires an optional dependency. "
                f"Install it with: pip install runspec-console[{provider}]"
            )
        except ValueError as exc:
            raise ValueError(f"LLM configuration error: {exc}") from exc
        # When a key-vending command is configured, don't cache the adapter — the
        # adapter's own TTL controls refresh. Static-key configs cache as before.
        if not llm_cfg.get("api_key_command"):
            self._adapter = adapter
        return adapter

    def _dispatch(self, event: str, detail: dict[str, Any]) -> None:
        if self._window is None:
            # Debug, not warning: the bridge logger propagates to runspec-core's
            # root stderr handler, so a warning here leaks to the launching
            # terminal. Background work can still race a not-yet-attached window;
            # that's expected, not worth shouting about.
            logger.debug("dispatch %s skipped: no window attached", event)
            return
        payload = json.dumps(detail)
        js = f"window.dispatchEvent(new CustomEvent({json.dumps(event)},{{detail:{payload}}}))"
        try:
            self._window.evaluate_js(js)
        except Exception:
            # Previously swallowed — that hid event-delivery failures (e.g. the
            # autonomy confirm prompt) and left nothing to diagnose. Log it.
            logger.exception("dispatch %s failed (evaluate_js raised)", event)

    @staticmethod
    def _current_user() -> str:
        try:
            import win32api  # type: ignore[import]

            # NameDisplay (3) returns the full display name, e.g. "Jason Finestone"
            return win32api.GetUserNameEx(3)  # type: ignore[attr-defined]
        except Exception:
            import os

            return os.environ.get("USERNAME", "user")


# ── helpers ───────────────────────────────────────────────────────────────────


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


PUTTY_PROXY_SESSION = "runspec-console-proxy"


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
    import shutil

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


def _remote_log_script(log_dir: str, runnable: str | None) -> str:
    """Shell snippet that frames and streams every matching log file over SSH.

    Each file is preceded by a ``\\x00RUNSPEC_LOG:<basename>`` frame (the name is
    only a fallback — attribution comes from the record body). Compacted
    ``.gz`` archives are decompressed with ``gzip -dc``; plain files are
    ``cat``-ed. Non-matching globs expand to themselves and are skipped by the
    ``[ -f ]`` guard, so no ``nullglob`` is required.
    """
    patterns = " ".join(f"{log_dir}/{g}" for g in _log_globs(runnable))
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
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _summary_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Pull the rich run_summary fields the Analytics tab needs.

    These are dropped from the lean HistoryRecord shape (used by the History
    tab) and only attached when ``include_extra=True`` is requested.
    """
    return {
        "autonomy": extra.get("autonomy"),
        "agent": bool(extra.get("agent")),
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


_EVENT_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _aggregate_analytics(
    records: list[dict[str, Any]],
    since: str,
    until: str,
    partial: bool = False,
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Fold parsed run_summary records into the Analytics data contract."""
    from datetime import date, timedelta

    daily: dict[str, dict[str, Any]] = {}
    buckets: dict[tuple, dict[str, Any]] = {}
    exceptions: dict[tuple, dict[str, Any]] = {}
    hosts_seen: set[str] = set()
    groups_seen: set[str] = set()
    runnables_seen: set[str] = set()
    operators_seen: set[str] = set()
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

        d = daily.setdefault(
            day, {"total": 0, "success": 0, "failure": 0, "durations": []}
        )
        d["total"] += 1
        d["success"] += 1 if ok else 0
        d["failure"] += 0 if ok else 1
        d["durations"].append(dur)

        bkey = (day, host, group, run, operator, initiated, autonomy)
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
            if isinstance(v, dict):
                inner = ", ".join(f"{ik} = {_toml_scalar(iv)}" for ik, iv in v.items())
                lines.append(f"{k} = {{ {inner} }}")
            else:
                lines.append(f"{k} = {_toml_scalar(v)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")

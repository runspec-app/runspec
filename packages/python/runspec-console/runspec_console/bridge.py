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
import hashlib
import json
import logging
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

# Autonomy escalation order — higher = more restrictive (must check before run).
_AUTONOMY_RANK = {"autonomous": 0, "confirm": 1, "supervised": 2, "manual": 3}


class Bridge:
    def __init__(self) -> None:
        self._window: Any = None
        self._lock = threading.Lock()
        self._in_flight: dict[str, dict[str, Any]] = {}
        self._adapter: Any = None  # ModelAdapter, loaded on demand
        self._hosts: list[dict[str, Any]] = []
        self._connected_cache: dict[str, bool] = {}  # host name → last known state
        self._runnables_cache: list[dict[str, Any]] = []
        # request_id → (Event, {"approved": bool}) for agent tool-call confirmations
        self._pending_confirms: dict[str, tuple[threading.Event, dict[str, bool]]] = {}
        self._reload_hosts()
        self._start_refresh_watcher()

    def set_window(self, window: Any) -> None:
        self._window = window

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
        pattern = f"{runnable}.log" if runnable else "*.log"
        for rp in paths:
            if not rp:
                continue
            log_dir = Path(rp).parent.parent / "logs"
            if not log_dir.exists():
                # Binary path (e.g. Scripts/runspec.exe) — fall back to ~/logs
                log_dir = Path.home() / "logs"
            if not log_dir.exists():
                continue
            for log_file in sorted(
                log_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True
            ):
                records.extend(_parse_log(log_file, host))
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return records[:200]

    def _get_remote_history(
        self, entry: dict[str, Any], runnable: str | None
    ) -> list[dict[str, Any]]:
        from pathlib import PurePosixPath
        from .executor import ssh_run

        ssh = entry["ssh"]
        idf = entry.get("identityFile")
        paths = _paths(entry)
        rp = paths[0] if paths else ""
        log_dir = str(PurePosixPath(rp).parent.parent / "logs")
        pattern = f"{log_dir}/{runnable}.log" if runnable else f"{log_dir}/*.log"
        script = (
            f"for f in {pattern}; do "
            f'[ -f "$f" ] && printf "\\x00RUNSPEC_LOG:%s\\n" "$(basename "$f" .log)" && cat "$f"; '
            f"done 2>/dev/null"
        )
        _, stdout, _ = ssh_run(
            ssh,
            script,
            identity_file=idf,
            global_ssh_config=self.get_config().get("ssh"),
            timeout=20,
        )
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

        # Step 5 — commit gate: all non-skipped hosts must be verified
        can_commit = all(h["verified"] or h["skipped"] for h in per_host)

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
        return {
            "ok": True,
            "committed": True,
            "public_key": public_key,
            "key_path": _normalize_path(str(canonical)),
            "per_host": per_host,
            "message": "Key rotated — pushed and verified on all hosts.",
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
            from .executor import ssh_run

            code, out, err = ssh_run(
                ssh,
                f"{rp} local --format json",
                identity_file=idf,
                global_ssh_config=self.get_config().get("ssh"),
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
        # If group not specified, look up the runnable's venv from the discovery cache
        if group is None and entry_paths:
            with self._lock:
                cached = next(
                    (
                        r
                        for r in self._runnables_cache
                        if r.get("host") == host and r.get("name") == runnable
                    ),
                    None,
                )
            if cached:
                group = cached.get("group")
        rp = next(
            (p for p in entry_paths if venv_name(p) == group),
            entry_paths[0] if entry_paths else "",
        )

        with self._lock:
            self._in_flight[inv_id] = {
                "id": inv_id,
                "runnable": runnable,
                "group": venv_name(rp) if rp else "",
                "host": host,
                "operator": self._current_user(),
                "runAs": "",
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
                )
            else:
                run_local(
                    rp, runnable, args, cp, on_line, on_done, cancel_event=cancel_event
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

        def run() -> None:
            try:
                adapter = self._get_adapter()
            except Exception as exc:
                self._dispatch("runspec:token", {"id": chat_id, "token": f"⚠ {exc}"})
                self._dispatch(
                    "runspec:run_end", {"id": chat_id, "exit_code": 1, "duration_ms": 0}
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
                    "runspec:run_end", {"id": chat_id, "exit_code": 1, "duration_ms": 0}
                )
                return
            asyncio.run(self._agentic_chat_turn(chat_id, message, adapter))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return chat_id

    async def _agentic_chat_turn(
        self, chat_id: str, message: str, adapter: Any
    ) -> None:
        start = time.monotonic()
        history: list[dict[str, Any]] = [{"role": "user", "content": message}]
        tools = self._runnables_to_tools()
        total_input = 0
        total_output = 0

        for _ in range(10):  # max agentic iterations
            response: Any = None
            try:
                async for event in adapter.stream_with_tools(history, tools):
                    if event[0] == "text":
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
                inp, out = self._usage_from_response(response)
                total_input += inp
                total_output += out

            if (
                response is None
                or response.stop_reason != "tool_use"
                or not response.tool_calls
            ):
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
                output = await self._gated_run_tool_async(tc.name, tc.input, chat_id)
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

        duration_ms = int((time.monotonic() - start) * 1000)
        self._dispatch(
            "runspec:run_end",
            {"id": chat_id, "exit_code": 0, "duration_ms": duration_ms},
        )
        if total_input or total_output:
            self._dispatch(
                "runspec:chat_usage",
                {
                    "id": chat_id,
                    "input_tokens": total_input,
                    "output_tokens": total_output,
                },
            )

    @staticmethod
    def _usage_from_response(response: Any) -> tuple[int, int]:
        raw = getattr(response, "_raw", None)
        if raw is None:
            return 0, 0
        usage = getattr(raw, "usage", None)
        if usage is None:
            return 0, 0
        inp = (
            getattr(usage, "input_tokens", None)
            or getattr(usage, "prompt_tokens", None)
            or 0
        )
        out = (
            getattr(usage, "output_tokens", None)
            or getattr(usage, "completion_tokens", None)
            or 0
        )
        return int(inp), int(out)

    def _runnables_to_tools(self) -> list[dict[str, Any]]:
        """Convert cached runnables to Anthropic-format tool schemas."""
        import re

        with self._lock:
            runnables = list(self._runnables_cache)
        tools: list[dict[str, Any]] = []
        for r in runnables:
            host = r.get("host", "local")
            name = r.get("name", "")
            raw_name = f"{host}__{name}"
            tool_name = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_name)[:64]
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
        holder = {"approved": False}
        with self._lock:
            self._pending_confirms[request_id] = (event, holder)
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
            if not self._finish_confirm(request_id, holder, timed_out):
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
            event, holder = entry
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
            )
        else:
            run_local(
                rp, runnable, tool_input, [], on_line, on_done, timeout=120, agent=True
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
        """Run a full refresh cycle immediately, then repeat every 30 s."""

        def _loop() -> None:
            while True:
                self._refresh_cycle()
                time.sleep(30)

        threading.Thread(target=_loop, daemon=True).start()

    def _refresh_cycle(self) -> None:
        """Concurrently probe connectivity, then concurrently discover runnables."""
        self._reload_hosts()
        local = self._local_host_entry()
        all_hosts = [local] + [h for h in self._hosts if h.get("name") != "local"]

        # ── Phase 1: connectivity probes (fast: ssh host true) ─────────────────
        with self._lock:
            self._connected_cache["local"] = True

        def probe(h: dict[str, Any]) -> None:
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

        # ── Phase 2: runnables discovery (heavier: runspec local --format json) ─
        discovered: list[dict[str, Any]] = []
        lock2 = threading.Lock()

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

        with self._lock:
            self._runnables_cache = discovered
        self._dispatch("runspec:runnables_updated", {})

    def _check_connected(self, host: dict[str, Any]) -> bool:
        ssh = host.get("ssh")
        if not ssh:
            return True
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
            logger.warning("dispatch %s dropped: no window attached", event)
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


def _parse_log(log_file: Path, host: str) -> list[dict[str, Any]]:
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return _parse_log_text(log_file.stem, text, host)


def _parse_log_text(name: str, text: str, host: str) -> list[dict[str, Any]]:
    """Parse a log file's text into HistoryRecord dicts.

    When run_id is present in records (runspec >=0.18) each invocation is
    isolated by its UUID — multi-user interleaving is handled cleanly.
    Older logs without run_id fall back to sequential accumulation between
    run_summary markers.
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
        return _parse_log_by_run_id(name, entries, host)
    return _parse_log_sequential(name, entries, host)


def _parse_log_by_run_id(
    name: str, entries: list[dict[str, Any]], host: str
) -> list[dict[str, Any]]:
    """Group log entries by run_id UUID → one HistoryRecord per invocation."""
    # Preserve insertion order of run_ids so history is chronological
    groups: dict[str, dict[str, Any]] = {}  # run_id → {"lines": [], "summary": None}
    for entry in entries:
        extra = entry.get("extra", {})
        run_id = extra.get("run_id")
        if not run_id:
            continue
        if run_id not in groups:
            groups[run_id] = {"lines": [], "summary": None}
        if extra.get("event") == "run_summary":
            groups[run_id]["summary"] = entry
        else:
            groups[run_id]["lines"].append(
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
        ts_raw = summary_entry.get("ts", "")
        stable_id = hashlib.md5((run_id + name).encode()).hexdigest()[:12]
        records.append(
            {
                "id": stable_id,
                "runnable": name,
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
        )
    return records


def _parse_log_sequential(
    name: str, entries: list[dict[str, Any]], host: str
) -> list[dict[str, Any]]:
    """Legacy parser for logs without run_id (runspec <0.18)."""
    records: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for entry in entries:
        extra = entry.get("extra", {})
        if extra.get("event") == "run_summary":
            ts_raw = entry.get("ts", "")
            stable_id = hashlib.md5((ts_raw + name).encode()).hexdigest()[:12]
            records.append(
                {
                    "id": stable_id,
                    "runnable": name,
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
            )
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

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
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from .config import config_path, hosts_path
from .discovery import discover_local, discover_remote
from .executor import args_to_argv, run_local, run_remote
from .hosts import load_hosts, save_hosts, venv_name


class Bridge:
    def __init__(self) -> None:
        self._window: Any = None
        self._lock = threading.Lock()
        self._in_flight: dict[str, dict[str, Any]] = {}
        self._adapter: Any = None        # ModelAdapter, loaded on demand
        self._hosts: list[dict[str, Any]] = []
        self._reload_hosts()

    def set_window(self, window: Any) -> None:
        self._window = window

    # ── hosts ────────────────────────────────────────────────────────────────

    def get_hosts(self) -> list[dict[str, Any]]:
        self._reload_hosts()
        local = self._local_host_entry()
        all_hosts = [local] + [h for h in self._hosts if h.get("name") != "local"]
        result: list[dict[str, Any]] = []
        for h in all_hosts:
            rp = h.get("runspec_path", "")
            result.append({
                "name": h["name"],
                "connected": self._check_connected(h),
                "runnableCount": 0,   # filled by get_runnables
                "groups": [venv_name(rp)] if rp else [],
                "role": h.get("role"),
                "group": h.get("group"),
            })
        return result

    def get_runnables(self, host: str) -> list[dict[str, Any]]:
        if host == "all":
            self._reload_hosts()
            local = self._local_host_entry()
            all_hosts = [local] + [h for h in self._hosts if h.get("name") != "local"]
            result: list[dict[str, Any]] = []
            for h in all_hosts:
                rp = h.get("runspec_path", "")
                ssh = h.get("ssh")
                name = h["name"]
                idf = h.get("identityFile")
                if ssh:
                    result.extend(discover_remote(ssh, rp, name, idf))
                else:
                    result.extend(discover_local(rp, name))
            return result
        entry = self._host_entry(host)
        if entry is None:
            return []
        rp = entry.get("runspec_path", "")
        ssh = entry.get("ssh")
        idf = entry.get("identityFile")
        if ssh:
            return discover_remote(ssh, rp, host, idf)
        return discover_local(rp, host)

    # ── history ───────────────────────────────────────────────────────────────

    def get_history(self, host: str, runnable: str | None = None) -> list[dict[str, Any]]:
        entry = self._host_entry(host)
        if entry is None:
            return []
        if entry.get("ssh"):
            return self._get_remote_history(entry, runnable)
        rp = entry.get("runspec_path", "")
        log_dir = Path(rp).parent.parent / "logs"
        if not log_dir.exists():
            return []
        records: list[dict[str, Any]] = []
        pattern = f"{runnable}.log" if runnable else "*.log"
        for log_file in sorted(log_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True):
            records.extend(_parse_log(log_file, host))
        return records[:200]

    def _get_remote_history(self, entry: dict[str, Any], runnable: str | None) -> list[dict[str, Any]]:
        from pathlib import PurePosixPath
        import subprocess as _sp
        from .executor import ssh_flags
        ssh = entry["ssh"]
        idf = entry.get("identityFile")
        rp = entry.get("runspec_path", "")
        log_dir = str(PurePosixPath(rp).parent.parent / "logs")
        pattern = f"{log_dir}/{runnable}.log" if runnable else f"{log_dir}/*.log"
        # One SSH call: emit a marker line before each file then its content
        script = (
            f'for f in {pattern}; do '
            f'[ -f "$f" ] && printf "\\x00RUNSPEC_LOG:%s\\n" "$(basename "$f" .log)" && cat "$f"; '
            f'done 2>/dev/null'
        )
        try:
            result = _sp.run(
                ["ssh", *ssh_flags(idf), ssh, script],
                capture_output=True, text=True, timeout=20,
                encoding="utf-8", errors="replace",
            )
        except Exception:
            return []
        records: list[dict[str, Any]] = []
        current_name: str | None = None
        current_lines: list[str] = []
        for line in result.stdout.splitlines():
            if line.startswith("\x00RUNSPEC_LOG:"):
                if current_name is not None:
                    records.extend(_parse_log_text(current_name, "\n".join(current_lines), entry["name"]))
                current_name = line[len("\x00RUNSPEC_LOG:"):]
                current_lines = []
            else:
                current_lines.append(line)
        if current_name is not None:
            records.extend(_parse_log_text(current_name, "\n".join(current_lines), entry["name"]))
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
        rp = entry.get("runspec_path", "")
        today_file = Path(rp).parent.parent / "runspec_today.json"
        if not today_file.exists():
            return None
        try:
            return json.loads(today_file.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ── config ────────────────────────────────────────────────────────────────

    def get_config(self) -> dict[str, Any]:
        path = config_path()
        if not path.exists():
            return {}
        try:
            with open(path, "rb") as f:
                return dict(tomllib.load(f))
        except Exception:
            return {}

    def save_config(self, data: dict[str, Any]) -> None:
        path = config_path()
        path.write_text(_dict_to_toml(data), encoding="utf-8")
        self._adapter = None

    # ── hosts file management ─────────────────────────────────────────────────

    def test_host(self, name: str) -> dict[str, Any]:
        entry = self._host_entry(name)
        if entry is None:
            return {"connected": False, "runspec_ok": False, "runnable_count": 0,
                    "stdout": "", "stderr": f"Host '{name}' not found in hosts file", "exit_code": -1}
        import subprocess as _sp
        ssh = entry.get("ssh")
        rp = entry.get("runspec_path", "")
        idf = entry.get("identityFile")
        if ssh:
            from .executor import ssh_flags
            cmd = ["ssh", *ssh_flags(idf), ssh, rp, "local", "--format", "json"]
        else:
            cmd = [rp, "local", "--format", "json"]
        try:
            r = _sp.run(cmd, capture_output=True, text=True, timeout=15,
                        encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return {"connected": bool(ssh), "runspec_ok": False, "runnable_count": 0,
                    "stdout": "", "stderr": f"Command not found: {cmd[0]}", "exit_code": -1}
        except _sp.TimeoutExpired:
            return {"connected": False, "runspec_ok": False, "runnable_count": 0,
                    "stdout": "", "stderr": "Timed out after 15s", "exit_code": -1}
        except Exception as exc:
            return {"connected": False, "runspec_ok": False, "runnable_count": 0,
                    "stdout": "", "stderr": str(exc), "exit_code": -1}
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
                "runspec_path": h.get("runspec_path", ""),
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
            entry: dict[str, Any] = {
                "name": h["name"],
                "ssh": ssh,
                "runspec_path": h.get("runspec_path", "runspec"),
            }
            if h.get("identityFile"):
                entry["identityFile"] = h["identityFile"]
            if h.get("group"):
                entry["group"] = h["group"]
            entries.append(entry)
        save_hosts(hosts_path(), entries)
        self._reload_hosts()

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
        return new

    # ── invocation ────────────────────────────────────────────────────────────

    def invoke_runnable(
        self,
        host: str,
        runnable: str,
        args: dict[str, Any],
        command_path: list[str] | None = None,
    ) -> str:
        inv_id = uuid.uuid4().hex[:12]
        cp = command_path or []
        entry = self._host_entry(host)

        with self._lock:
            self._in_flight[inv_id] = {
                "id": inv_id,
                "runnable": runnable,
                "group": venv_name(entry["runspec_path"]) if entry else "",
                "host": host,
                "operator": self._current_user(),
                "runAs": "",
                "startedAt": _iso_now(),
                "args": args,
            }

        def run() -> None:
            def on_line(line: str, stream: str) -> None:
                self._dispatch("runspec:output", {"id": inv_id, "line": line, "stream": stream})

            def on_done(exit_code: int, duration_ms: int) -> None:
                with self._lock:
                    self._in_flight.pop(inv_id, None)
                self._dispatch("runspec:run_end", {"id": inv_id, "exit_code": exit_code, "duration_ms": duration_ms})

            if entry is None:
                on_line(f"✗  Host '{host}' not found in runspec_hosts.toml", "stderr")
                on_done(-1, 0)
                return

            rp = entry["runspec_path"]
            ssh = entry.get("ssh")
            if ssh:
                run_remote(ssh, rp, runnable, args, cp, on_line, on_done, entry.get("identityFile"))
            else:
                run_local(rp, runnable, args, cp, on_line, on_done)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return inv_id

    def get_in_flight(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._in_flight.values())

    # ── chat ──────────────────────────────────────────────────────────────────

    def send_chat(self, message: str, invocation_id: str | None = None) -> str:
        chat_id = uuid.uuid4().hex[:12]

        def run() -> None:
            adapter = self._get_adapter()
            if adapter is None:
                self._dispatch("runspec:token", {"id": chat_id, "token": "⚠ No LLM provider configured. Set provider and API key in Settings."})
                self._dispatch("runspec:run_end", {"id": chat_id, "exit_code": 1, "duration_ms": 0})
                return
            asyncio.run(self._chat_turn(chat_id, message, adapter))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return chat_id

    async def _chat_turn(self, chat_id: str, message: str, adapter: Any) -> None:
        start = time.monotonic()
        messages = [{"role": "user", "content": message}]
        try:
            response = await adapter.chat(messages, tools=[])
            if response.text:
                for token in response.text.split(" "):
                    self._dispatch("runspec:token", {"id": chat_id, "token": token + " "})
                    await asyncio.sleep(0)
        except Exception as exc:
            self._dispatch("runspec:token", {"id": chat_id, "token": f"⚠ LLM error: {exc}"})
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            self._dispatch("runspec:run_end", {"id": chat_id, "exit_code": 0, "duration_ms": duration_ms})

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
                return {"name": "local", "runspec_path": str(candidate), "ssh": None}
        # Fallback: construct the expected path even if not yet installed
        return {"name": "local", "runspec_path": str(scripts / "runspec.exe"), "ssh": None}

    def _host_entry(self, host: str) -> dict[str, Any] | None:
        if host == "local":
            return self._local_host_entry()
        return next((h for h in self._hosts if h.get("name") == host), None)

    def _check_connected(self, host: dict[str, Any]) -> bool:
        ssh = host.get("ssh")
        if not ssh:
            return True   # local is always connected
        from .executor import ssh_flags
        idf = host.get("identityFile")
        import subprocess
        try:
            r = subprocess.run(
                ["ssh", *ssh_flags(idf), "-o", "ConnectTimeout=3", ssh, "true"],
                capture_output=True, timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

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
        try:
            from .adapters.base import load_adapter
            self._adapter = load_adapter(provider, **kwargs)
        except (ImportError, ValueError):
            pass
        return self._adapter

    def _dispatch(self, event: str, detail: dict[str, Any]) -> None:
        if self._window is None:
            return
        payload = json.dumps(detail)
        js = f"window.dispatchEvent(new CustomEvent({json.dumps(event)},{{detail:{payload}}}))"
        try:
            self._window.evaluate_js(js)
        except Exception:
            pass

    @staticmethod
    def _current_user() -> str:
        try:
            import win32api  # type: ignore[import]
            return win32api.GetUserName()
        except Exception:
            import os
            return os.environ.get("USERNAME", "user")


# ── helpers ───────────────────────────────────────────────────────────────────


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
    records: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    try:
        for raw in text.splitlines():
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            extra = entry.get("extra", {})
            if extra.get("event") == "run_summary":
                ts_raw = entry.get("ts", "")
                stable_id = hashlib.md5((ts_raw + name).encode()).hexdigest()[:12]
                records.append({
                    "id": stable_id,
                    "runnable": name,
                    "group": "",
                    "host": host,
                    "operator": extra.get("user", ""),
                    "runAs": extra.get("user_target") or "",
                    "exitCode": extra.get("exit_code", 0),
                    "durationMs": extra.get("duration_ms", 0),
                    "ts": entry.get("ts", ""),
                    "args": extra.get("args", {}),
                    "logLines": lines,
                })
                lines = []
            else:
                lines.append({
                    "ts": entry.get("ts", ""),
                    "level": entry.get("level", "INFO"),
                    "message": entry.get("message", raw),
                })
    except Exception:
        pass
    return records


def _dict_to_toml(data: dict[str, Any], _prefix: str = "") -> str:
    """Minimal TOML serialiser for flat and one-level-nested dicts."""
    lines: list[str] = []
    nested: list[tuple[str, dict[str, Any]]] = []
    for k, v in data.items():
        if isinstance(v, dict):
            nested.append((k, v))
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        elif isinstance(v, bool):
            lines.append(f'{k} = {"true" if v else "false"}')
        elif v is None:
            pass
        else:
            lines.append(f"{k} = {v}")
    for section, sub in nested:
        lines.append(f"\n[{section}]")
        for k, v in sub.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            elif isinstance(v, bool):
                lines.append(f'{k} = {"true" if v else "false"}')
            elif v is not None:
                lines.append(f"{k} = {v}")
    return "\n".join(lines) + "\n"


def _write_schedules(path: Path, schedules: list[dict[str, Any]]) -> None:
    lines = []
    for s in schedules:
        lines.append("[[schedule]]")
        for k, v in s.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            elif isinstance(v, bool):
                lines.append(f'{k} = {"true" if v else "false"}')
            else:
                lines.append(f"{k} = {v}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")

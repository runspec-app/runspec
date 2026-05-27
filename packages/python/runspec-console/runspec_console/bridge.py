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

from .config import config_path, hosts_path, _dict_to_toml, read_config, write_config
from .discovery import discover_local, discover_remote
from .executor import args_to_argv, run_local, run_remote
from .hosts import load_hosts, save_hosts, venv_name

_CANCEL_KEY = "__cancel_event__"   # key in _in_flight dicts, not surfaced to JS


class Bridge:
    def __init__(self) -> None:
        self._window: Any = None
        self._lock = threading.Lock()
        self._in_flight: dict[str, dict[str, Any]] = {}
        self._adapter: Any = None        # ModelAdapter, loaded on demand
        self._hosts: list[dict[str, Any]] = []
        self._connected_cache: dict[str, bool] = {}   # host name → last known state
        self._runnables_cache: list[dict[str, Any]] = []
        self._reload_hosts()
        self._start_refresh_watcher()

    def set_window(self, window: Any) -> None:
        self._window = window
        self._maximized = False

    # ── window controls (frameless) ───────────────────────────────────────────

    def minimize_window(self) -> None:
        if self._window:
            self._window.minimize()

    def toggle_maximize_window(self) -> None:
        if not self._window:
            return
        if self._maximized:
            self._window.restore()
            self._maximized = False
        else:
            self._window.maximize()
            self._maximized = True

    def close_window(self) -> None:
        if self._window:
            self._window.destroy()

    def resize_window(self, width: int, height: int) -> None:
        if self._window:
            self._window.resize(int(width), int(height))

    def move_window(self, x: int, y: int) -> None:
        if self._window:
            self._window.move(int(x), int(y))

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
            result.append({
                "name": h["name"],
                "connected": connected,
                "runnableCount": 0,   # filled by get_runnables
                "groups": [venv_name(p) for p in paths],
                "role": h.get("role"),
                "group": h.get("group"),
            })
        return result

    def get_runnables(self, host: str) -> list[dict[str, Any]]:
        with self._lock:
            cache = list(self._runnables_cache)
        if host == "all":
            return cache
        return [r for r in cache if r.get("host") == host]

    # ── history ───────────────────────────────────────────────────────────────

    def get_history(self, host: str, runnable: str | None = None) -> list[dict[str, Any]]:
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
                continue
            for log_file in sorted(log_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True):
                records.extend(_parse_log(log_file, host))
        records.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return records[:200]

    def _get_remote_history(self, entry: dict[str, Any], runnable: str | None) -> list[dict[str, Any]]:
        from pathlib import PurePosixPath
        import subprocess as _sp
        from .executor import ssh_flags
        ssh = entry["ssh"]
        idf = entry.get("identityFile")
        paths = _paths(entry)
        rp = paths[0] if paths else ""
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
        paths = _paths(entry)
        rp = next((p for p in paths if venv_name(p) == group), paths[0] if paths else "")
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

    def save_config(self, data: dict[str, Any]) -> None:
        write_config(data)
        self._adapter = None

    def generate_ssh_key(self, key_path: str) -> dict[str, Any]:
        from .tools.generate_ssh_key import _run_keygen
        from datetime import datetime, timezone
        result = _run_keygen(key_path)
        if result["ok"]:
            cfg = self.get_config()
            ssh_section = dict(cfg.get("ssh", {}))
            ssh_section["key_created_at"] = datetime.now(timezone.utc).isoformat()
            cfg["ssh"] = ssh_section
            self.save_config(cfg)
        return result

    # ── hosts file management ─────────────────────────────────────────────────

    def test_host(self, name: str) -> dict[str, Any]:
        entry = self._host_entry(name)
        if entry is None:
            return {"connected": False, "runspec_ok": False, "runnable_count": 0,
                    "stdout": "", "stderr": f"Host '{name}' not found in hosts file", "exit_code": -1}
        import subprocess as _sp
        ssh = entry.get("ssh")
        paths = _paths(entry)
        rp = paths[0] if paths else ""
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
            entry: dict[str, Any] = {
                "name": h["name"],
                "ssh": ssh,
                "runspec_paths": raw_paths or ["runspec"],
            }
            if h.get("identityFile"):
                entry["identityFile"] = h["identityFile"]
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
                cached = next((r for r in self._runnables_cache
                               if r.get("host") == host and r.get("name") == runnable), None)
            if cached:
                group = cached.get("group")
        rp = next((p for p in entry_paths if venv_name(p) == group), entry_paths[0] if entry_paths else "")

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
                self._dispatch("runspec:output", {"id": inv_id, "line": line, "stream": stream})

            def on_done(exit_code: int, duration_ms: int) -> None:
                with self._lock:
                    self._in_flight.pop(inv_id, None)
                self._dispatch("runspec:run_end", {"id": inv_id, "exit_code": exit_code, "duration_ms": duration_ms})

            if entry is None or not rp:
                on_line(f"✗  Host '{host}' not found in runspec_hosts.toml", "stderr")
                on_done(-1, 0)
                return

            ssh = entry.get("ssh")
            if ssh:
                run_remote(ssh, rp, runnable, args, cp, on_line, on_done,
                           entry.get("identityFile"), cancel_event=cancel_event)
            else:
                run_local(rp, runnable, args, cp, on_line, on_done, cancel_event=cancel_event)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return inv_id

    def get_in_flight(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{k: v for k, v in inv.items() if k != _CANCEL_KEY}
                    for inv in self._in_flight.values()]

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
            adapter = self._get_adapter()
            if adapter is None:
                self._dispatch("runspec:token", {"id": chat_id, "token": "⚠ No LLM provider configured. Set provider and API key in Settings."})
                self._dispatch("runspec:run_end", {"id": chat_id, "exit_code": 1, "duration_ms": 0})
                return
            asyncio.run(self._agentic_chat_turn(chat_id, message, adapter))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return chat_id

    async def _agentic_chat_turn(self, chat_id: str, message: str, adapter: Any) -> None:
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
                        self._dispatch("runspec:token", {"id": chat_id, "token": event[1]})
                    elif event[0] == "done":
                        response = event[1]
            except Exception as exc:
                self._dispatch("runspec:token", {"id": chat_id, "token": f"\n⚠ Error: {exc}"})
                break

            if response is not None:
                inp, out = self._usage_from_response(response)
                total_input += inp
                total_output += out

            if response is None or response.stop_reason != "tool_use" or not response.tool_calls:
                break

            # Execute each tool call and dispatch events
            tool_results: list[tuple[Any, str]] = []
            for tc in response.tool_calls:
                self._dispatch("runspec:tool_start", {
                    "id": chat_id, "tool_name": tc.name, "tool_input": tc.input,
                })
                output = await asyncio.to_thread(self._run_tool_sync, tc.name, tc.input)
                self._dispatch("runspec:tool_end", {
                    "id": chat_id, "tool_name": tc.name, "output": output[:2000],
                })
                tool_results.append((tc, output))

            history.extend(adapter.make_tool_turn(response, tool_results))

        duration_ms = int((time.monotonic() - start) * 1000)
        self._dispatch("runspec:run_end", {"id": chat_id, "exit_code": 0, "duration_ms": duration_ms})
        if total_input or total_output:
            self._dispatch("runspec:chat_usage", {
                "id": chat_id, "input_tokens": total_input, "output_tokens": total_output,
            })

    @staticmethod
    def _usage_from_response(response: Any) -> tuple[int, int]:
        raw = getattr(response, "_raw", None)
        if raw is None:
            return 0, 0
        usage = getattr(raw, "usage", None)
        if usage is None:
            return 0, 0
        inp = getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", None) or 0
        out = getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", None) or 0
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
                    "int": "integer", "float": "number",
                    "flag": "boolean", "bool": "boolean", "path": "string",
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
            tools.append({"name": tool_name, "description": description, "input_schema": schema})
        return tools

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
            cached_r = next((r for r in self._runnables_cache
                             if r.get("host") == host and r.get("name") == runnable), None)
        run_group = cached_r.get("group") if cached_r else None
        rp = next((p for p in tool_paths if venv_name(p) == run_group), tool_paths[0] if tool_paths else "")
        ssh = entry.get("ssh")
        if ssh:
            run_remote(ssh, rp, runnable, tool_input, [], on_line, on_done,
                       entry.get("identityFile"), timeout=120)
        else:
            run_local(rp, runnable, tool_input, [], on_line, on_done, timeout=120)

        output = "\n".join(output_lines)
        if len(output) > 16384:
            output = output[:16384] + "\n[output truncated]"
        if exit_code_holder[0] != 0:
            return f"[exit {exit_code_holder[0]}]\n{output}"
        return output or "(no output)"

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
        return {"name": "local", "runspec_paths": [str(scripts / "runspec.exe")], "ssh": None}

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
            for h in all_hosts if h.get("ssh")
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
                items = discover_remote(ssh, rp, name, idf) if ssh else discover_local(rp, name)
                with lock2:
                    discovered.extend(items)

        disc_threads = [
            threading.Thread(target=discover, args=(h,), daemon=True)
            for h in all_hosts
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


def _paths(entry: dict[str, Any]) -> list[str]:
    """Normalize runspec_paths (list) or legacy runspec_path (str) to a list."""
    paths = entry.get("runspec_paths")
    if paths and isinstance(paths, list):
        return [str(p) for p in paths if p]
    rp = entry.get("runspec_path", "")
    return [str(rp)] if rp else []


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


def _parse_log_by_run_id(name: str, entries: list[dict[str, Any]], host: str) -> list[dict[str, Any]]:
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
            groups[run_id]["lines"].append({
                "ts": entry.get("ts", ""),
                "level": entry.get("level", "INFO"),
                "message": entry.get("message", ""),
            })

    records: list[dict[str, Any]] = []
    for run_id, g in groups.items():
        summary_entry = g["summary"]
        if summary_entry is None:
            continue   # in-progress run — no summary yet
        extra = summary_entry.get("extra", {})
        ts_raw = summary_entry.get("ts", "")
        stable_id = hashlib.md5((run_id + name).encode()).hexdigest()[:12]
        records.append({
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
        })
    return records


def _parse_log_sequential(name: str, entries: list[dict[str, Any]], host: str) -> list[dict[str, Any]]:
    """Legacy parser for logs without run_id (runspec <0.18)."""
    records: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for entry in entries:
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
                "ts": ts_raw,
                "args": extra.get("args", {}),
                "argSources": extra.get("arg_sources", {}),
                "logLines": lines,
            })
            lines = []
        else:
            lines.append({
                "ts": entry.get("ts", ""),
                "level": entry.get("level", "INFO"),
                "message": entry.get("message", ""),
            })
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

"""
test_phoenix.py — the local Arize Phoenix server manager.

Phoenix itself is never installed or launched in CI: the subprocess spawn and the
port probe are monkeypatched, and ``available()`` is forced where a test needs the
"installed" world. This keeps the suite light and hermetic while still exercising
start / stop / status, the double-start guard, adoption of an already-serving port,
crash detection, and the launch env vars.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

import pytest

from runspec_console import phoenix as phx


class _FakeProc:
    """Minimal subprocess.Popen stand-in."""

    def __init__(self, alive: bool = True) -> None:
        self._alive = alive
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout: float | None = None) -> int:
        self._alive = False
        return 0


def _force_available(monkeypatch: pytest.MonkeyPatch, ok: bool = True) -> None:
    monkeypatch.setattr(
        phx, "available", lambda: (ok, "" if ok else "Phoenix not installed — x")
    )


def test_available_reason_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # available() probes with find_spec (never imports phoenix) — simulate absence
    # regardless of the real environment.
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    ok, reason = phx.available()
    assert ok is False
    assert "pip install runspec-console[phoenix]" in reason


def test_available_true_when_spec_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # A found spec ⇒ installed; find_spec is used, so the package is never executed.
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert phx.available() == (True, "")


def test_available_treats_broken_install_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    def boom(name: str) -> Any:
        raise ImportError("partial install")

    monkeypatch.setattr(importlib.util, "find_spec", boom)
    ok, _ = phx.available()
    assert ok is False


def test_traces_endpoint() -> None:
    assert phx.traces_endpoint(6006) == "http://127.0.0.1:6006/v1/traces"


def test_status_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_available(monkeypatch, ok=False)
    server = phx.PhoenixServer()
    st = server.status()
    assert st == {
        "available": False,
        "reason": "Phoenix not installed — x",
        "running": False,
        "url": "",
        "port": 0,
    }


def test_start_unavailable_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_available(monkeypatch, ok=False)
    spawned = []
    monkeypatch.setattr(
        phx.subprocess, "Popen", lambda *a, **k: spawned.append(a) or _FakeProc()
    )
    server = phx.PhoenixServer()
    st = server.start({"port": 6006, "working_dir": ""})
    assert st["running"] is False
    assert spawned == []  # never tried to launch


def test_start_success_sets_running_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_available(monkeypatch, ok=True)
    captured: dict[str, Any] = {}

    def fake_popen(cmd: list[str], env: dict[str, str], **k: Any) -> _FakeProc:
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeProc(alive=True)

    monkeypatch.setattr(phx.subprocess, "Popen", fake_popen)
    # Pin the launch argv so the assertion doesn't depend on whether a `phoenix`
    # console script happens to sit next to the test interpreter.
    monkeypatch.setattr(phx, "_phoenix_argv", lambda: ["phoenix", "serve"])
    # Port closed before launch (so we don't "adopt"); HTTP ready after (came up).
    monkeypatch.setattr(phx, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(phx, "_http_ready", lambda *a, **k: True)

    server = phx.PhoenixServer()
    st = server.start({"port": 6007, "working_dir": ""})

    assert st["running"] is True
    assert st["port"] == 6007
    assert st["url"] == "http://127.0.0.1:6007"
    assert captured["cmd"] == ["phoenix", "serve"]
    assert captured["env"]["PHOENIX_PORT"] == "6007"
    assert captured["env"]["PHOENIX_HOST"] == "127.0.0.1"
    # Assistant is disabled by default (local-privacy stance).
    assert captured["env"]["PHOENIX_DISABLE_AGENT_ASSISTANT"] == "true"


def test_assistant_knob_and_env_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_available(monkeypatch, ok=True)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        phx.subprocess,
        "Popen",
        lambda cmd, env, **k: captured.update(env=env) or _FakeProc(),
    )
    monkeypatch.setattr(phx, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(phx, "_http_ready", lambda *a, **k: True)
    server = phx.PhoenixServer()
    server.start(
        {
            "port": 6006,
            "working_dir": "",
            "assistant": True,  # opt back in
            "env": {"PHOENIX_ENABLE_MCP_SERVER": "false"},  # passthrough
        }
    )
    # assistant=True flips the disable flag off...
    assert captured["env"]["PHOENIX_DISABLE_AGENT_ASSISTANT"] == "false"
    # ...and the passthrough var is applied verbatim.
    assert captured["env"]["PHOENIX_ENABLE_MCP_SERVER"] == "false"


def test_working_dir_sets_env_and_is_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _force_available(monkeypatch, ok=True)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        phx.subprocess,
        "Popen",
        lambda cmd, env, **k: captured.update(env=env) or _FakeProc(),
    )
    monkeypatch.setattr(phx, "_port_open", lambda *a, **k: False)  # don't adopt
    monkeypatch.setattr(phx, "_http_ready", lambda *a, **k: True)  # comes up
    wd = tmp_path / "phoenix"
    server = phx.PhoenixServer()
    server.start({"port": 6006, "working_dir": str(wd)})
    assert wd.is_dir()
    assert captured["env"]["PHOENIX_WORKING_DIR"] == str(wd)


def test_double_start_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_available(monkeypatch, ok=True)
    spawn_count = {"n": 0}

    def fake_popen(*a: Any, **k: Any) -> _FakeProc:
        spawn_count["n"] += 1
        return _FakeProc(alive=True)

    monkeypatch.setattr(phx.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(phx, "_port_open", lambda *a, **k: False)  # don't adopt
    monkeypatch.setattr(phx, "_http_ready", lambda *a, **k: True)  # comes up
    server = phx.PhoenixServer()
    server.start({"port": 6006, "working_dir": ""})
    server.start({"port": 6006, "working_dir": ""})  # second call is a no-op
    assert spawn_count["n"] == 1


def test_adopt_already_serving_port(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_available(monkeypatch, ok=True)
    spawned = []
    monkeypatch.setattr(
        phx.subprocess, "Popen", lambda *a, **k: spawned.append(1) or _FakeProc()
    )
    # Port already open before launch → adopt it, never spawn.
    monkeypatch.setattr(phx, "_port_open", lambda *a, **k: True)
    server = phx.PhoenixServer()
    st = server.start({"port": 6006, "working_dir": ""})
    # Adopted server is reported usable (so the tab embeds it) even though we
    # don't own the process — and we never spawned one.
    assert st["running"] is True
    assert st["port"] == 6006
    assert st["url"] == "http://127.0.0.1:6006"
    assert spawned == []


def test_adopted_server_not_killed_on_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_available(monkeypatch, ok=True)
    spawned = []
    monkeypatch.setattr(
        phx.subprocess, "Popen", lambda *a, **k: spawned.append(1) or _FakeProc()
    )
    monkeypatch.setattr(phx, "_port_open", lambda *a, **k: True)  # already serving
    server = phx.PhoenixServer()
    server.start({"port": 6006, "working_dir": ""})
    st = server.stop()  # must not raise; nothing of ours to kill
    assert spawned == []
    assert st["running"] is False


def test_start_crash_before_up(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_available(monkeypatch, ok=True)
    monkeypatch.setattr(phx.subprocess, "Popen", lambda *a, **k: _FakeProc(alive=False))
    # Port never opens; the fake proc reports dead → _wait_until_up returns False.
    monkeypatch.setattr(phx, "_port_open", lambda *a, **k: False)
    server = phx.PhoenixServer()
    st = server.start({"port": 6006, "working_dir": ""})
    assert st["running"] is False


def test_stop_terminates_managed_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_available(monkeypatch, ok=True)
    proc = _FakeProc(alive=True)
    monkeypatch.setattr(phx.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(phx, "_port_open", lambda *a, **k: False)  # don't adopt
    monkeypatch.setattr(phx, "_http_ready", lambda *a, **k: True)  # comes up
    server = phx.PhoenixServer()
    server.start({"port": 6006, "working_dir": ""})
    assert server.is_running() is True
    st = server.stop()
    assert proc.terminated is True
    assert st["running"] is False
    assert st["port"] == 0


def test_stop_when_never_started_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_available(monkeypatch, ok=True)
    server = phx.PhoenixServer()
    st = server.stop()  # no proc — must not raise
    assert st["running"] is False


def test_launch_exception_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_available(monkeypatch, ok=True)

    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("cannot exec")

    monkeypatch.setattr(phx.subprocess, "Popen", boom)
    monkeypatch.setattr(phx, "_port_open", lambda *a, **k: False)
    server = phx.PhoenixServer()
    st = server.start({"port": 6006, "working_dir": ""})  # must not raise
    assert st["running"] is False


def test_terminate_kill_fallback_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_available(monkeypatch, ok=True)

    class _StubbornProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(alive=True)
            self._waits = 0

        def wait(self, timeout: float | None = None) -> int:
            self._waits += 1
            if self._waits == 1:
                raise subprocess.TimeoutExpired("phoenix", timeout or 0)
            self._alive = False
            return 0

    proc = _StubbornProc()
    monkeypatch.setattr(phx.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(phx, "_port_open", lambda *a, **k: False)  # don't adopt
    monkeypatch.setattr(phx, "_http_ready", lambda *a, **k: True)  # comes up
    server = phx.PhoenixServer()
    server.start({"port": 6006, "working_dir": ""})
    server.stop()
    assert proc.terminated is True
    assert proc.killed is True  # escalated to kill after the terminate timed out


# ── launch argv (#the fix): prefer the `phoenix` console script ────────────────


def test_phoenix_argv_prefers_console_script(monkeypatch, tmp_path):
    """When a `phoenix`/`phoenix.exe` sits next to the interpreter (a normal venv
    with the extra installed), launch *that* — the same entry `phoenix serve` uses
    — not `python -m phoenix.server.main` (which misbehaves under Windows spawn)."""
    fake_python = tmp_path / ("python.exe" if os.name == "nt" else "python")
    fake_python.write_text("")
    script_name = "phoenix.exe" if os.name == "nt" else "phoenix"
    script = tmp_path / script_name
    script.write_text("")
    monkeypatch.setattr(phx.sys, "executable", str(fake_python))
    assert phx._phoenix_argv() == [str(script), "serve"]


def test_phoenix_argv_fallback_when_no_script(monkeypatch, tmp_path):
    fake_python = tmp_path / "python"
    fake_python.write_text("")
    monkeypatch.setattr(phx.sys, "executable", str(fake_python))
    assert phx._phoenix_argv() == [
        str(fake_python),
        "-m",
        "phoenix.server.main",
        "serve",
    ]


def test_start_captures_server_log(monkeypatch, tmp_path):
    """The server's stdout+stderr are captured to {working_dir}/phoenix-server.log
    (not DEVNULL) so a failed/slow start is diagnosable."""
    _force_available(monkeypatch, ok=True)
    monkeypatch.setattr(phx, "_phoenix_argv", lambda: ["phoenix", "serve"])
    monkeypatch.setattr(phx, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(phx, "_http_ready", lambda *a, **k: True)
    captured: dict[str, Any] = {}

    def fake_popen(cmd, env, **k):
        captured["stdout"] = k.get("stdout")
        captured["stderr"] = k.get("stderr")
        return _FakeProc(alive=True)

    monkeypatch.setattr(phx.subprocess, "Popen", fake_popen)
    server = phx.PhoenixServer()
    wd = tmp_path / "phx"
    server.start({"port": 6006, "working_dir": str(wd)})
    log = wd / "phoenix-server.log"
    assert log.exists()
    assert captured["stdout"] is not None
    assert captured["stdout"] is not phx.subprocess.DEVNULL
    assert captured["stderr"] is phx.subprocess.STDOUT
    server.stop()  # closes the file handle


def test_start_honours_startup_timeout(monkeypatch):
    _force_available(monkeypatch, ok=True)
    monkeypatch.setattr(phx, "_phoenix_argv", lambda: ["phoenix", "serve"])
    monkeypatch.setattr(phx, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(phx.subprocess, "Popen", lambda *a, **k: _FakeProc(alive=True))

    server = phx.PhoenixServer()
    seen: dict[str, float] = {}
    monkeypatch.setattr(
        server, "_wait_until_up", lambda port, timeout: seen.update(t=timeout) or True
    )
    server.start({"port": 6006, "working_dir": "", "startup_timeout": 12.5})
    assert seen["t"] == 12.5

    # Absent ⇒ the module default.
    seen.clear()
    server.stop()
    monkeypatch.setattr(
        server, "_wait_until_up", lambda port, timeout: seen.update(t=timeout) or True
    )
    server.start({"port": 6006, "working_dir": ""})
    assert seen["t"] == phx._DEFAULT_START_TIMEOUT_S


# ── auto-wire: local Phoenix captures content by default ───────────────────────


def test_route_traces_to_phoenix_captures_content(monkeypatch):
    """Routing to local Phoenix overlays enabled + endpoint, clears auth, and
    takes capture_content from [phoenix] (default true) — NOT the [telemetry]
    value (default false, for the external path). Other telemetry keys carry over."""
    from runspec_console import config as config_mod
    from runspec_console import observability
    from runspec_console.bridge_observability import ObservabilityMixin

    monkeypatch.setattr(
        config_mod,
        "read_telemetry",
        lambda: {
            "enabled": False,
            "endpoint": None,
            "headers": {"x-api-key": "secret"},
            "service_name": "svc",
            "sample_ratio": 0.5,
            "capture_content": False,  # the external-path default
            "api_key_command": "keyring get x",
            "ca_bundle": "/ca.pem",
            "tls_verify": False,
        },
    )
    seen: dict[str, Any] = {}
    monkeypatch.setattr(observability, "reconfigure", lambda cfg: seen.update(cfg))

    # Default [phoenix] ⇒ capture_content True.
    monkeypatch.setattr(config_mod, "read_phoenix", lambda: {"capture_content": True})
    ObservabilityMixin._route_traces_to_phoenix(object(), 6006)
    assert seen["enabled"] is True
    assert seen["endpoint"] == "http://127.0.0.1:6006/v1/traces"
    assert (
        seen["capture_content"] is True
    )  # from [phoenix], overriding telemetry's False
    assert seen["service_name"] == "svc"  # carried over
    assert seen["sample_ratio"] == 0.5  # carried over
    assert seen["headers"] == {}  # cleared — loopback, no auth
    assert seen["api_key_command"] is None
    assert seen["ca_bundle"] is None

    # Opt out via [phoenix] capture_content = false.
    seen.clear()
    monkeypatch.setattr(config_mod, "read_phoenix", lambda: {"capture_content": False})
    ObservabilityMixin._route_traces_to_phoenix(object(), 6006)
    assert seen["capture_content"] is False

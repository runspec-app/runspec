"""
test_mcp_venv.py — the "install a stdio MCP server into a dedicated venv" helper
(pure path/normalisation logic) and the Bridge.mcp_install_venv orchestration
(with the subprocess mocked, so no real network/venv).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from runspec_console import config as config_mod
from runspec_console import mcp_venv
from runspec_console.bridge import Bridge

# ── pure helpers ────────────────────────────────────────────────────────────────


def test_slugify():
    assert mcp_venv.slugify("Code Graph!") == "code-graph"
    assert mcp_venv.slugify("  a/b:c  ") == "a-b-c"
    assert mcp_venv.slugify("") == "server"
    assert mcp_venv.slugify("!!!") == "server"


def test_venv_paths(tmp_path):
    v = mcp_venv.venv_dir(tmp_path, "Code Graph")
    assert v == tmp_path / "mcp-venvs" / "code-graph"
    # bin/Scripts split by platform.
    assert mcp_venv.venv_python(v).parent == mcp_venv.bin_dir(v)
    assert mcp_venv.venv_python(v).name in ("python", "python.exe")
    assert mcp_venv.launcher_path(v, "codebase-memory-mcp").name in (
        "codebase-memory-mcp",
        "codebase-memory-mcp.exe",
    )


def test_default_command():
    assert mcp_venv.default_command("codebase-memory-mcp") == "codebase-memory-mcp"
    assert (
        mcp_venv.default_command("codebase-memory-mcp==0.10.8") == "codebase-memory-mcp"
    )
    assert mcp_venv.default_command("foo[extras]>=1,<2") == "foo"
    assert mcp_venv.default_command("pkg @ https://x/y.whl") == "pkg"
    assert mcp_venv.default_command("") == ""


def test_is_safe_package_spec():
    assert mcp_venv.is_safe_package_spec("codebase-memory-mcp")
    assert mcp_venv.is_safe_package_spec("foo[bar]==1.2")
    assert not mcp_venv.is_safe_package_spec("--index-url http://evil")
    assert not mcp_venv.is_safe_package_spec("-e .")
    assert not mcp_venv.is_safe_package_spec("a b")
    assert not mcp_venv.is_safe_package_spec("")
    assert not mcp_venv.is_safe_package_spec("https://x/y.whl")


def test_installed_scripts_skips_stock(tmp_path):
    v = tmp_path / "venv"
    d = mcp_venv.bin_dir(v)
    d.mkdir(parents=True)
    for n in ("python", "pip", "activate", "_private", "codebase-memory-mcp", "other"):
        (d / n).write_text("")
    scripts = mcp_venv.installed_scripts(v)
    assert "codebase-memory-mcp" in scripts and "other" in scripts
    assert (
        "python" not in scripts and "pip" not in scripts and "_private" not in scripts
    )


# ── Bridge.mcp_install_venv (subprocess mocked) ─────────────────────────────────


class _McpBridge:
    mcp_install_venv = Bridge.mcp_install_venv
    save_mcp_config = Bridge.save_mcp_config

    def __init__(self):
        self.written: dict | None = None

    def mcp_reload(self):
        return {"available": True, "servers": []}


def _install(monkeypatch, tmp_path, payload, *, pip_rc=0, make_launcher=True):
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(
        config_mod, "read_mcp", lambda: {"enabled": True, "servers": []}
    )
    captured: dict = {}
    monkeypatch.setattr(
        config_mod, "write_mcp", lambda section: captured.update(section or {})
    )

    real_run = subprocess.run

    def fake_run(argv, **kw):
        venv = mcp_venv.venv_dir(tmp_path, payload["name"])
        if argv[1:3] == ["-m", "venv"]:
            mcp_venv.venv_python(venv).parent.mkdir(parents=True, exist_ok=True)
            mcp_venv.venv_python(venv).write_text("")  # so py.exists()
            return subprocess.CompletedProcess(argv, 0, "created", "")
        if "pip" in argv:
            if make_launcher:
                cmd = payload.get("command") or mcp_venv.default_command(
                    payload["package"]
                )
                lp = mcp_venv.launcher_path(venv, cmd)
                lp.parent.mkdir(parents=True, exist_ok=True)
                lp.write_text("")
            return subprocess.CompletedProcess(argv, pip_rc, "installed", "")
        return real_run(argv, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return _McpBridge().mcp_install_venv(payload), captured


def test_install_happy_path(monkeypatch, tmp_path):
    res, written = _install(
        monkeypatch,
        tmp_path,
        {"name": "codebase-graph", "package": "codebase-memory-mcp"},
    )
    assert res["ok"] is True
    assert res["server"] == "codebase-graph"
    # The server was wired into [mcp] as a stdio entry pointing at the venv launcher.
    servers = written["servers"]
    assert len(servers) == 1
    entry = servers[0]
    assert entry["name"] == "codebase-graph"
    assert entry["transport"] == "stdio"
    assert entry["enabled"] is True
    assert entry["autonomy"] == "confirm"
    assert entry["command"] == res["command"]
    assert str(mcp_venv.venv_dir(tmp_path, "codebase-graph")) in entry["command"]
    assert Path(entry["command"]).exists()


def test_install_rejects_unsafe_package(monkeypatch, tmp_path):
    res, _ = _install(
        monkeypatch, tmp_path, {"name": "x", "package": "--index-url http://evil"}
    )
    assert res["ok"] is False and "plain named pip spec" in res["error"]


def test_install_requires_name_and_package(monkeypatch, tmp_path):
    r1, _ = _install(monkeypatch, tmp_path, {"name": "", "package": "p"})
    r2, _ = _install(monkeypatch, tmp_path, {"name": "n", "package": ""})
    assert r1["ok"] is False and "name" in r1["error"].lower()
    assert r2["ok"] is False and "package" in r2["error"].lower()


def test_install_pip_failure_surfaces(monkeypatch, tmp_path):
    res, _ = _install(
        monkeypatch, tmp_path, {"name": "x", "package": "nope-nonexistent"}, pip_rc=1
    )
    assert res["ok"] is False and "pip install failed" in res["error"]


def test_install_missing_launcher_lists_scripts(monkeypatch, tmp_path):
    # Package installs but its launcher name differs from the guess → operator picks.
    res, _ = _install(
        monkeypatch,
        tmp_path,
        {"name": "x", "package": "some-pkg", "command": "wrong-name"},
        make_launcher=False,
    )
    assert res["ok"] is False
    assert "launcher" in res["error"].lower()
    assert "scripts" in res

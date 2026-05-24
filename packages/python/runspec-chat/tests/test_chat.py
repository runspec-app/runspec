"""
Tests for chat.py — the main() launch path and _sync_user_env helper.

The smoke test in CI only verifies wheel contents and --help (which exits
before the launch path runs). These tests cover the code that actually
matters at runtime: the env var that tells Chainlit where to find its
config, and the user_env sync logic.
"""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from runspec_chat import chat


# ── helpers ───────────────────────────────────────────────────────────────────


def _write_config(path: Path, user_env: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"user_env = {json.dumps(user_env)}\n")


def _fake_run(captured: dict):
    """Return a subprocess.run replacement that records the cmd and env."""

    def run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["CHAINLIT_APP_ROOT"] = os.environ.get("CHAINLIT_APP_ROOT")
        captured["RUNSPEC_CHAT_MODEL"] = os.environ.get("RUNSPEC_CHAT_MODEL")
        return SimpleNamespace(returncode=0)

    return run


# ── _sync_user_env ────────────────────────────────────────────────────────────


def test_sync_user_env_no_hosts_file(tmp_path):
    cfg = tmp_path / "config.toml"
    _write_config(cfg, ["ANTHROPIC_API_KEY"])

    chat._sync_user_env(tmp_path / "jump_hosts.toml", cfg)

    text = cfg.read_text()
    assert 'user_env = ["ANTHROPIC_API_KEY"]' in text


def test_sync_user_env_shared_ssh(tmp_path):
    cfg = tmp_path / "config.toml"
    _write_config(cfg, ["ANTHROPIC_API_KEY"])
    hosts = tmp_path / "jump_hosts.toml"
    hosts.write_text("[hosts.myserver]\nssh = \"myserver.example.com\"\n")

    chat._sync_user_env(hosts, cfg)

    env = json.loads(cfg.read_text().split("user_env = ")[1].strip())
    assert "ANTHROPIC_API_KEY" in env
    assert "SSH_PASS" in env


def test_sync_user_env_per_host_user(tmp_path):
    cfg = tmp_path / "config.toml"
    _write_config(cfg, ["ANTHROPIC_API_KEY"])
    hosts = tmp_path / "jump_hosts.toml"
    hosts.write_text("[hosts.my-server]\nssh = \"my-server.example.com\"\nuser = \"admin\"\n")

    chat._sync_user_env(hosts, cfg)

    env = json.loads(cfg.read_text().split("user_env = ")[1].strip())
    assert "ANTHROPIC_API_KEY" in env
    assert "SSH_MY_SERVER_PASS" in env
    assert "SSH_PASS" not in env


def test_sync_user_env_non_ssh_host_ignored(tmp_path):
    cfg = tmp_path / "config.toml"
    _write_config(cfg, ["ANTHROPIC_API_KEY"])
    hosts = tmp_path / "jump_hosts.toml"
    hosts.write_text("[hosts.local]\nbin = \"/usr/local/bin/runspec\"\n")

    chat._sync_user_env(hosts, cfg)

    env = json.loads(cfg.read_text().split("user_env = ")[1].strip())
    assert env == ["ANTHROPIC_API_KEY"]


# ── main() ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Prevent env vars set by main() from leaking between tests."""
    for key in ("CHAINLIT_APP_ROOT", "RUNSPEC_CHAT_MODEL", "RUNSPEC_CHAT_HOSTS"):
        monkeypatch.delenv(key, raising=False)


def test_main_sets_chainlit_app_root(monkeypatch):
    """CHAINLIT_APP_ROOT must point to a dir containing .chainlit/config.toml and chainlit.md.

    This is the regression test for the CHAINLIT_ROOT vs CHAINLIT_APP_ROOT
    typo: if the wrong env var name is used, Chainlit silently ignores it and
    runs with defaults — no settings panel, no MCP plug icon.
    """
    monkeypatch.setattr(sys, "argv", ["runspec-chat"])
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _fake_run(captured))

    with pytest.raises(SystemExit) as exc:
        chat.main()

    assert exc.value.code == 0
    assert "CHAINLIT_APP_ROOT" in captured, "subprocess.run was never called"
    root = Path(captured["CHAINLIT_APP_ROOT"])
    assert (root / ".chainlit" / "config.toml").is_file(), "config.toml missing from CHAINLIT_APP_ROOT"
    assert (root / "chainlit.md").is_file(), "chainlit.md missing from CHAINLIT_APP_ROOT"


def test_main_config_has_mcp_enabled(monkeypatch):
    """The bundled config.toml must have [features.mcp] enabled — controls the MCP plug icon."""
    monkeypatch.setattr(sys, "argv", ["runspec-chat"])
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _fake_run(captured))

    with pytest.raises(SystemExit):
        chat.main()

    config_path = Path(captured["CHAINLIT_APP_ROOT"]) / ".chainlit" / "config.toml"
    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    assert config.get("features", {}).get("mcp", {}).get("enabled") is True


def test_main_config_has_user_env(monkeypatch):
    """The bundled config.toml must declare at least ANTHROPIC_API_KEY in user_env."""
    monkeypatch.setattr(sys, "argv", ["runspec-chat"])
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _fake_run(captured))

    with pytest.raises(SystemExit):
        chat.main()

    config_path = Path(captured["CHAINLIT_APP_ROOT"]) / ".chainlit" / "config.toml"
    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    assert "ANTHROPIC_API_KEY" in config.get("project", {}).get("user_env", [])


def test_main_cmd_includes_app_py_and_defaults(monkeypatch):
    """Subprocess command must include app.py, default port 8000, and default host."""
    monkeypatch.setattr(sys, "argv", ["runspec-chat"])
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _fake_run(captured))

    with pytest.raises(SystemExit):
        chat.main()

    cmd = captured["cmd"]
    assert any(str(c).endswith("app.py") for c in cmd)
    assert "8000" in cmd
    assert "0.0.0.0" in cmd


def test_main_watch_flag_forwarded(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["runspec-chat", "--watch"])
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _fake_run(captured))

    with pytest.raises(SystemExit):
        chat.main()

    assert "--watch" in captured["cmd"]


def test_main_headless_flag_forwarded(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["runspec-chat", "--headless"])
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _fake_run(captured))

    with pytest.raises(SystemExit):
        chat.main()

    assert "--headless" in captured["cmd"]


def test_main_custom_port_and_host(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["runspec-chat", "--port", "9000", "--host", "127.0.0.1"])
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _fake_run(captured))

    with pytest.raises(SystemExit):
        chat.main()

    assert "9000" in captured["cmd"]
    assert "127.0.0.1" in captured["cmd"]


def test_main_model_env_var_set(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["runspec-chat", "--model", "claude-opus-4-7"])
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _fake_run(captured))

    with pytest.raises(SystemExit):
        chat.main()

    assert captured["RUNSPEC_CHAT_MODEL"] == "claude-opus-4-7"

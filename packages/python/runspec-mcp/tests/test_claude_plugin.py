"""The Claude Code plugin's two install routes: the runspec.app zip (uvx) and
the pip route (``runspec-mcp claude-plugin``), plus the published marketplaces."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

from runspec_mcp import claudeplugin, cli
from runspec_mcp import config as cfgmod

ROOT = Path(__file__).resolve().parents[4]
BUILDER = ROOT / "scripts" / "build_claude_marketplace.py"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_render_sets_server_hook_and_version(tmp_path):
    dest = claudeplugin.render(tmp_path / "p", server=["/v/bin/runspec-mcp", "serve"], hook_python="/v/bin/python", version="9.9.9")
    manifest = _read(dest / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == "runspec-mcp" and manifest["version"] == "9.9.9"
    fleet = manifest["mcpServers"]["fleet"]
    assert fleet["command"] == "/v/bin/runspec-mcp" and fleet["args"] == ["serve"]
    # Install-time settings reach the gateway through the PLUGIN_ variables.
    assert fleet["env"] == {
        "RUNSPEC_MCP_PLUGIN_CONFIG_DIR": "${user_config.config_dir}",
        "RUNSPEC_MCP_PLUGIN_CONFIG": "${user_config.config_file}",
    }
    assert set(manifest["userConfig"]) == {"config_dir", "config_file"}
    hook = _read(dest / "hooks" / "hooks.json")["hooks"]["PreToolUse"][0]
    assert hook["hooks"][0]["command"] == '"/v/bin/python" "${CLAUDE_PLUGIN_ROOT}/hooks/autonomy_gate.py"'
    assert hook["matcher"] == "mcp__plugin_runspec[-_]mcp_.*"  # unchanged: same plugin name either route
    assert (dest / "hooks" / "autonomy_gate.py").is_file() and (dest / "commands").is_dir()
    assert not list(dest.rglob("__pycache__"))


def test_render_replaces_a_previous_copy(tmp_path):
    dest = tmp_path / "p"
    claudeplugin.render(dest, server=["a", "serve"])
    (dest / "stale.txt").write_text("x")
    claudeplugin.render(dest, server=["b", "serve"])
    assert not (dest / "stale.txt").exists()
    assert _read(dest / ".claude-plugin" / "plugin.json")["mcpServers"]["fleet"]["command"] == "b"


def test_claude_plugin_command_renders_for_this_install_once(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("RUNSPEC_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_gateway_bin", lambda: "/abs/bin/runspec-mcp")
    assert cli.main(["claude-plugin"]) == 0
    path = Path(capsys.readouterr().out.strip())
    assert path.is_absolute() and path.parent == tmp_path / "claude-plugin"
    manifest = _read(path / ".claude-plugin" / "plugin.json")
    assert manifest["mcpServers"]["fleet"]["command"] == "/abs/bin/runspec-mcp"
    hook = _read(path / "hooks" / "hooks.json")["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert hook.startswith(f'"{sys.executable}" ')

    # Claude Code runs the command once per session: an unchanged install is
    # not re-rendered (the files are left alone)…
    marker = path / "marker"
    marker.write_text("kept")
    assert cli.main(["claude-plugin"]) == 0
    assert marker.exists() and Path(capsys.readouterr().out.strip()) == path
    # …but a moved install is.
    monkeypatch.setattr(cli, "_gateway_bin", lambda: "/elsewhere/runspec-mcp")
    assert cli.main(["claude-plugin"]) == 0
    capsys.readouterr()
    assert not marker.exists()
    assert _read(path / ".claude-plugin" / "plugin.json")["mcpServers"]["fleet"]["command"] == "/elsewhere/runspec-mcp"


def test_plugin_settings_reach_the_config_resolution(monkeypatch, tmp_path):
    for var in ("RUNSPEC_MCP_CONFIG_DIR", "RUNSPEC_MCP_CONFIG", "RUNSPEC_MCP_PLUGIN_CONFIG_DIR", "RUNSPEC_MCP_PLUGIN_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("RUNSPEC_MCP_PLUGIN_CONFIG_DIR", str(tmp_path / "console"))
    assert cfgmod.default_config_dir() == tmp_path / "console"
    # A value set by hand wins over the plugin's install-time setting…
    monkeypatch.setenv("RUNSPEC_MCP_CONFIG_DIR", str(tmp_path / "manual"))
    assert cfgmod.default_config_dir() == tmp_path / "manual"
    monkeypatch.delenv("RUNSPEC_MCP_CONFIG_DIR")
    # …and an unexpanded placeholder (plugin loaded without settings) or an
    # empty setting means "not set".
    for value in ("${user_config.config_dir}", "", "  "):
        monkeypatch.setenv("RUNSPEC_MCP_PLUGIN_CONFIG_DIR", value)
        assert cfgmod.default_config_dir() is None
    monkeypatch.setenv("RUNSPEC_MCP_PLUGIN_CONFIG", str(tmp_path / "x.toml"))
    assert cfgmod.default_config_path() == tmp_path / "x.toml"
    monkeypatch.setenv("RUNSPEC_MCP_PLUGIN_CONFIG", "${user_config.config_file}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUNSPEC_MCP_HOME", str(tmp_path / "home"))
    assert cfgmod.default_config_path() is None


def _builder():
    spec = importlib.util.spec_from_file_location("build_claude_marketplace", BUILDER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_marketplaces_build_with_a_matching_checksum(tmp_path):
    mod = _builder()
    written = mod.build(tmp_path, "https://runspec.app")
    version = mod._version()
    zip_path = written["zip"]
    assert zip_path == tmp_path / "plugins" / f"runspec-mcp-{version}.zip"

    market = _read(tmp_path / "marketplace.json")
    assert market["name"] == "runspec"
    (entry,) = market["plugins"]
    assert entry["name"] == "runspec-mcp"
    assert entry["source"] == {
        "source": "archive",
        "url": f"https://runspec.app/plugins/runspec-mcp-{version}.zip",
        "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
    }

    pip = _read(tmp_path / "marketplace-pip.json")
    assert pip["name"] == "runspec-pip"
    (entry,) = pip["plugins"]
    assert entry["name"] == "runspec-mcp"
    assert entry["source"] == {"source": "command", "command": "runspec-mcp claude-plugin", "timeout": 60}

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert "runspec-mcp/.claude-plugin/plugin.json" in names
        manifest = json.loads(zf.read("runspec-mcp/.claude-plugin/plugin.json"))
    assert manifest["version"] == version
    fleet = manifest["mcpServers"]["fleet"]
    assert [fleet["command"], *fleet["args"]] == claudeplugin.UVX_COMMAND


def test_the_zip_is_byte_identical_across_builds(tmp_path):
    mod = _builder()
    a = mod.build(tmp_path / "a", "https://runspec.app")["zip"].read_bytes()
    b = mod.build(tmp_path / "b", "https://runspec.app")["zip"].read_bytes()
    assert a == b  # a docs redeploy with no plugin change publishes the same checksum


def test_marketplace_names_are_not_reserved():
    # Claude Code reserves npm/pip/uv/… and the official names; ours are compound.
    for name in ("runspec", "runspec-pip"):
        assert name not in {"npm", "pip", "uv", "cargo", "github", "gh"}
        assert not name.startswith("claudeai-")

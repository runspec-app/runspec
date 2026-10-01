"""The connect/credential/manifest fixes: why a client couldn't connect or a
credential never reached a runnable."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from runspec_mcp import cli
from runspec_mcp import config as cfgmod
from runspec_mcp.config import load_config

HOOK = Path(cfgmod.__file__).parent / "plugin" / "hooks" / "autonomy_gate.py"


def test_a_credential_without_a_label_uses_its_id(tmp_path):
    # The docs' own example gives only `id`; that used to derive no env vars.
    p = tmp_path / "runspec_mcp.toml"
    p.write_text('[[credential]]\nid = "Windows"\nkind = "userpass"\nusername = "svc"\n\n[[credential]]\nid = "x"\nlabel = "Jira Dev"\nkind = "token"\n')
    creds = {c["id"]: c for c in load_config(config_path=p).credentials}
    assert creds["Windows"]["label"] == "Windows"
    assert creds["x"]["label"] == "Jira Dev"  # an explicit label still wins


def test_manifest_lives_in_the_app_dir_not_the_plugin(monkeypatch, tmp_path):
    monkeypatch.delenv("RUNSPEC_PLUGIN_MANIFEST", raising=False)
    monkeypatch.setenv("RUNSPEC_MCP_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/usr/lib/python3/site-packages/runspec_mcp/plugin")
    assert cfgmod.manifest_path() == tmp_path / "autonomy.json"


def _hook():
    spec = importlib.util.spec_from_file_location("autonomy_gate", HOOK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_hook_reads_where_the_gateway_writes(monkeypatch, tmp_path):
    hook = _hook()
    for env in ({"RUNSPEC_MCP_HOME": str(tmp_path)}, {"XDG_CONFIG_HOME": str(tmp_path)}):
        for k in ("RUNSPEC_PLUGIN_MANIFEST", "RUNSPEC_MCP_HOME", "XDG_CONFIG_HOME"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        assert hook.manifest_path() == cfgmod.manifest_path()
    monkeypatch.setenv("RUNSPEC_PLUGIN_MANIFEST", str(tmp_path / "m.json"))
    assert hook.manifest_path() == cfgmod.manifest_path() == tmp_path / "m.json"


def test_plugin_launch_config_sets_no_manifest_path():
    # The hook and the gateway must agree on the manifest's location, so the
    # plugin sets only its own config settings — never a manifest/home path.
    plugin = json.loads((HOOK.parent.parent / ".claude-plugin" / "plugin.json").read_text())
    env = plugin["mcpServers"]["fleet"].get("env", {})
    assert set(env) == {"RUNSPEC_MCP_PLUGIN_CONFIG_DIR", "RUNSPEC_MCP_PLUGIN_CONFIG"}


def test_setup_prints_absolute_paths(capsys, tmp_path):
    conf = tmp_path / "runspec_mcp.toml"
    conf.write_text("")
    assert cli.main(["setup", "vscode", "--config", str(conf)]) == 0
    out = capsys.readouterr().out
    server = json.loads(out[out.index("{") :])["servers"]["runspec"]
    assert Path(server["command"]).is_absolute() and server["args"] == ["serve"]
    assert server["env"]["RUNSPEC_MCP_CONFIG"] == str(conf.resolve())
    assert cli.main(["setup", "claude", "--config", str(conf)]) == 0
    out = capsys.readouterr().out
    assert "claude mcp add runspec" in out and f"RUNSPEC_MCP_CONFIG={conf.resolve()}" in out
    assert cli.main(["setup", "json", "--config", str(conf)]) == 0
    assert "mcpServers" in json.loads(capsys.readouterr().out)


def test_keychain_reason_names_the_real_cause(monkeypatch):
    monkeypatch.setattr(cli.cred, "_keyring", lambda: None)
    assert "runspec-mcp[credentials]" in cli._no_keychain_reason()
    monkeypatch.setattr(cli.cred, "_keyring", lambda: object())
    monkeypatch.setattr(cli.sys, "platform", "linux")
    assert "Secret Service" in cli._no_keychain_reason()

"""A console config directory reused by an MCP client, and the per-host key."""

from __future__ import annotations

import json

from runspec_mcp import cli
from runspec_mcp.config import load_config

HOSTS = """
[[host]]
name = "deb2-alice"
ssh = "alice@deb2"
runspec_paths = ["/opt/venvs/ops/bin/runspec"]
identityFile = "C:/Users/me/AppData/Roaming/runspec-console/runspec_ed25519"

[[host]]
name = "deb3"
ssh = "jason@deb3"
runspec_paths = ["/opt/venvs/admin/bin/runspec"]
"""


def _console_dir(tmp_path):
    d = tmp_path / "runspec-console"
    d.mkdir()
    (d / "runspec_hosts.toml").write_text(HOSTS)
    (d / "config.toml").write_text('[ssh]\nidentityFile = "C:/keys/console_ed25519"\n')
    return d


def test_console_per_host_identity_file_is_honoured(tmp_path):
    cfg = load_config(config_dir=_console_dir(tmp_path))
    hosts = {h["name"]: h for h in cfg.hosts}
    assert hosts["deb2-alice"]["identity_file"] == "C:/Users/me/AppData/Roaming/runspec-console/runspec_ed25519"
    assert "identity_file" not in hosts["deb3"]  # falls back to the global [ssh] key
    assert cfg.ssh["identityFile"] == "C:/keys/console_ed25519"


def test_either_spelling_works_in_runspec_mcp_toml(tmp_path):
    p = tmp_path / "runspec_mcp.toml"
    p.write_text('[[host]]\nname = "a"\nssh = "u@a"\nidentityFile = "/k/a"\n\n[[host]]\nname = "b"\nssh = "u@b"\nidentity_file = "/k/b"\n')
    hosts = {h["name"]: h for h in load_config(config_path=p).hosts}
    assert hosts["a"]["identity_file"] == "/k/a"
    assert hosts["b"]["identity_file"] == "/k/b"


def test_the_catalog_passes_the_host_key_to_ssh(tmp_path):
    from runspec_mcp.catalog import _discover_items

    seen = {}

    class Pool:
        def run(self, target, cmd, identity_file=None, global_ssh_config=None, timeout=None):
            seen["identity"] = identity_file
            return 0, "[]", ""

    cfg = load_config(config_dir=_console_dir(tmp_path))
    host = next(h for h in cfg.hosts if h["name"] == "deb2-alice")
    _discover_items(host, "/opt/venvs/ops/bin/runspec", cfg.ssh, Pool())
    assert seen["identity"].endswith("runspec_ed25519")


def test_config_dir_from_the_environment(tmp_path, monkeypatch):
    # What an MCP client that passes env but no args (the Claude Code plugin) uses.
    monkeypatch.setenv("RUNSPEC_MCP_CONFIG_DIR", str(_console_dir(tmp_path)))
    cfg = load_config()
    assert {h["name"] for h in cfg.hosts} == {"deb2-alice", "deb3"}
    assert cfg.source.endswith("runspec-console")


def test_an_explicit_config_beats_the_environment_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNSPEC_MCP_CONFIG_DIR", str(_console_dir(tmp_path)))
    p = tmp_path / "runspec_mcp.toml"
    p.write_text('[[host]]\nname = "only"\nssh = "u@only"\n')
    assert [h["name"] for h in load_config(config_path=p).hosts] == ["only"]


def test_serve_accepts_config_dir(tmp_path, monkeypatch):
    # The command an MCP client runs: `runspec-mcp serve --config-dir <dir>`.
    loaded = {}
    monkeypatch.setattr(cli, "cmd_serve", lambda args: loaded.setdefault("cfg", cli._load(args)) and 0)
    assert cli.main(["serve", "--config-dir", str(_console_dir(tmp_path))]) == 0
    assert {h["name"] for h in loaded["cfg"].hosts} == {"deb2-alice", "deb3"}


def test_setup_points_clients_at_a_config_dir(tmp_path, capsys):
    d = _console_dir(tmp_path)
    assert cli.main(["setup", "vscode", "--config-dir", str(d)]) == 0
    out = capsys.readouterr().out
    server = json.loads(out[out.index("{") :])["servers"]["runspec"]
    assert server["args"] == ["serve"]
    assert server["env"] == {"RUNSPEC_MCP_CONFIG_DIR": str(d.resolve())}
    assert cli.main(["setup", "claude", "--config-dir", str(d)]) == 0
    assert f"RUNSPEC_MCP_CONFIG_DIR={d.resolve()}" in capsys.readouterr().out

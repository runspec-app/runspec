"""Config resolution: single-file and console config-dir."""

from __future__ import annotations

from runspec_mcp.config import load_config


def test_single_file(tmp_path):
    (tmp_path / "runspec_mcp.toml").write_text(
        """
[local]
venv_globs = ["~/venvs/*"]

[gateway]
refresh_interval = 5

[[host]]
name = "prod"
ssh = "u@prod"
runspec_paths = ["/opt/v/bin/runspec"]

[[credential]]
id = "Windows"
label = "Windows"
kind = "userpass"
username = "svc"
""",
        encoding="utf-8",
    )
    cfg = load_config(config_path=tmp_path / "runspec_mcp.toml")
    assert [h["name"] for h in cfg.hosts] == ["prod"]
    assert cfg.credentials[0]["id"] == "Windows"
    assert cfg.local_venv_globs == ["~/venvs/*"]
    assert cfg.refresh_interval == 5


def test_console_config_dir(tmp_path):
    (tmp_path / "runspec_hosts.toml").write_text(
        '[[host]]\nname = "prod"\nssh = "u@prod"\nrunspec_paths = ["/opt/v/bin/runspec"]\n',
        encoding="utf-8",
    )
    (tmp_path / "runspec_credentials.toml").write_text(
        '[[credential]]\nid = "Jira"\nlabel = "Jira"\nkind = "userpass"\nusername = "svc"\n',
        encoding="utf-8",
    )
    (tmp_path / "config.toml").write_text(
        "[ssh]\nmax_concurrent = 4\n[local]\nvenv_globs = ['C:/venvs/*']\n",
        encoding="utf-8",
    )
    cfg = load_config(config_dir=tmp_path)
    assert [h["name"] for h in cfg.hosts] == ["prod"]
    assert cfg.credentials[0]["id"] == "Jira"
    assert cfg.ssh.get("max_concurrent") == 4
    assert cfg.local_venv_globs == ["C:/venvs/*"]


def test_empty_config(tmp_path):
    cfg = load_config(config_path=tmp_path / "missing.toml")
    assert cfg.hosts == []
    assert cfg.credentials == []


def test_fleet_section_from_env(tmp_path, monkeypatch):
    # [gateway] must be read from the resolved config even when it comes from
    # $RUNSPEC_MCP_CONFIG (no explicit --config) — the refresh_interval bug.
    p = tmp_path / "runspec_mcp.toml"
    p.write_text("[gateway]\nrefresh_interval = 7\nserver_name = 'myfleet'\n", encoding="utf-8")
    monkeypatch.setenv("RUNSPEC_MCP_CONFIG", str(p))
    cfg = load_config()  # no config_path — resolves via the env var
    assert cfg.refresh_interval == 7
    assert cfg.server_name == "myfleet"

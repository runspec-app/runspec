"""
test_generate_ssh_key_tool.py — the `generate-ssh-key` CLI tool tags its public
key with the install's marker and persists the .pub, so a key bootstrapped from
the command line is recognised (and pruned) by later rotations exactly like one
generated from Settings → SSH.
"""

from __future__ import annotations

from runspec_console import config
from runspec_console.tools import generate_ssh_key as tool


def test_generate_writes_tagged_pub_and_ppk_comment(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_app_slug", lambda: "jason-console")
    monkeypatch.setattr(tool, "app_dir", lambda: tmp_path)
    res = tool._generate(rotate=False)
    assert res["ok"]
    assert res["public_key"].startswith("ssh-ed25519 ")
    assert res["public_key"].endswith(" jason-console")
    assert (tmp_path / "runspec_ed25519.pub").read_text().strip() == res["public_key"]
    ppk = (tmp_path / "runspec_ed25519.ppk").read_text()
    assert "Comment: jason-console" in ppk


def test_generate_default_install_uses_default_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_app_slug", lambda: "runspec-console")
    monkeypatch.setattr(tool, "app_dir", lambda: tmp_path)
    res = tool._generate(rotate=False)
    assert res["public_key"].endswith(" runspec-console")

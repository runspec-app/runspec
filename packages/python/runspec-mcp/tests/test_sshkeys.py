"""Managed SSH keypair: generation, idempotency, default-identity wiring, copy cmd."""

from __future__ import annotations

import stat
import sys

import pytest

from runspec_mcp import sshkeys
from runspec_mcp.config import FleetConfig


@pytest.fixture(autouse=True)
def _isolate_app_dir(tmp_path, monkeypatch):
    # Point the managed key at a throwaway app dir for every test.
    monkeypatch.setenv("RUNSPEC_MCP_HOME", str(tmp_path))
    yield


def test_ensure_managed_key_generates_ed25519_once():
    priv = sshkeys.ensure_managed_key()
    assert priv.exists()
    pub = sshkeys.managed_public_key()
    assert pub.startswith("ssh-ed25519 ")
    assert pub.endswith(sshkeys.KEY_MARKER)

    # Idempotent: a second call must not replace the key.
    before = priv.read_bytes()
    again = sshkeys.ensure_managed_key()
    assert again == priv
    assert priv.read_bytes() == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX perms")
def test_private_key_is_0600():
    priv = sshkeys.ensure_managed_key()
    mode = stat.S_IMODE(priv.stat().st_mode)
    assert mode == 0o600


def test_apply_managed_identity_sets_default_for_remote_fleet():
    cfg = FleetConfig(hosts=[{"name": "prod", "ssh": "u@prod"}], ssh={})
    sshkeys.apply_managed_identity(cfg)
    assert cfg.ssh["identityFile"] == str(sshkeys.managed_key_path())


def test_apply_managed_identity_respects_operator_identity():
    cfg = FleetConfig(hosts=[{"name": "prod", "ssh": "u@prod"}], ssh={"identityFile": "~/mine"})
    sshkeys.apply_managed_identity(cfg)
    assert cfg.ssh["identityFile"] == "~/mine"


def test_apply_managed_identity_noop_for_local_only_fleet():
    cfg = FleetConfig(hosts=[{"name": "local"}], ssh={})
    sshkeys.apply_managed_identity(cfg)
    assert "identityFile" not in cfg.ssh
    # And no key was generated for a local-only fleet.
    assert not sshkeys.managed_key_path().exists()


def test_authorize_command_is_idempotent_and_quoted():
    cmd = sshkeys.authorize_command("ssh-ed25519 AAAA runspec-mcp")
    assert "authorized_keys" in cmd
    assert "grep -qF 'ssh-ed25519 AAAA runspec-mcp'" in cmd
    assert "chmod 700 ~/.ssh" in cmd
    assert "chmod 600 ~/.ssh/authorized_keys" in cmd

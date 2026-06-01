"""
test_key_pubkey.py — .pub persistence + get_public_key derivation.

Binds the real bridge methods onto a light stub so we avoid Bridge.__init__'s
side effects. Uses real cryptography (a runtime dep, present via paramiko too).
"""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runspec_console.bridge import Bridge, _public_key_from_private


class _StubBridge:
    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg

    def get_config(self) -> dict:
        return self._cfg

    _generate_keypair_to_path = Bridge._generate_keypair_to_path
    get_public_key = Bridge.get_public_key


def _write_openssh_private(path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def test_public_key_from_private_derives(tmp_path):
    priv = tmp_path / "id_ed25519"
    _write_openssh_private(priv)
    pub = _public_key_from_private(priv)
    assert pub.startswith("ssh-ed25519 ")


def test_generate_keypair_writes_pub(tmp_path):
    dest = tmp_path / "runspec_ed25519"
    result = _StubBridge({})._generate_keypair_to_path(dest)
    assert result["ok"]
    pub_file = Path(str(dest) + ".pub")
    assert pub_file.is_file()
    assert pub_file.read_text(encoding="utf-8").strip() == result["public_key"]
    assert result["public_key"].startswith("ssh-ed25519 ")


def test_get_public_key_reads_existing_pub(tmp_path):
    priv = tmp_path / "id_ed25519"
    _write_openssh_private(priv)
    (tmp_path / "id_ed25519.pub").write_text(
        "ssh-ed25519 AAAA-from-pub-file\n", encoding="utf-8"
    )
    bridge = _StubBridge({"ssh": {"identityFile": str(priv)}})
    r = bridge.get_public_key()
    assert r["ok"]
    assert r["public_key"] == "ssh-ed25519 AAAA-from-pub-file"


def test_get_public_key_derives_and_writes_pub_when_missing(tmp_path):
    priv = tmp_path / "id_ed25519"
    _write_openssh_private(priv)
    pub_file = Path(str(priv) + ".pub")
    assert not pub_file.exists()
    bridge = _StubBridge({"ssh": {"identityFile": str(priv)}})
    r = bridge.get_public_key()
    assert r["ok"]
    assert r["public_key"].startswith("ssh-ed25519 ")
    # derivation also persisted the .pub for next time
    assert pub_file.is_file()
    assert pub_file.read_text(encoding="utf-8").strip() == r["public_key"]


def test_get_public_key_no_key_configured():
    assert _StubBridge({}).get_public_key()["ok"] is False


def test_get_public_key_missing_file(tmp_path):
    r = _StubBridge({"ssh": {"identityFile": str(tmp_path / "nope")}}).get_public_key()
    assert r["ok"] is False
    assert "not found" in r["message"]

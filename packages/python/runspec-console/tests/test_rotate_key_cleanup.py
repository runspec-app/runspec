"""
test_rotate_key_cleanup.py — rotation prunes the console's stale keys from a
host's authorized_keys instead of appending a new one every time.

The prune logic lives in a shell/awk command built by `_build_rotation_push_cmd`.
These tests run that *real* command through `/bin/sh` (with HOME pointed at a
temp dir) against a seeded authorized_keys and assert the resulting file — so
the actual shell behaviour is verified, not a string match. Skipped where sh or
awk isn't available (Windows CI runs the pure-Python tests below regardless).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runspec_console.bridge import Bridge
from runspec_console.bridge_config_ssh import (
    _KEY_MARKER,
    _build_rotation_push_cmd,
    _key_blob,
)

_SH = shutil.which("sh")
_AWK = shutil.which("awk")
needs_sh = pytest.mark.skipif(not (_SH and _AWK), reason="needs sh + awk")


def _run(cmd: str, home: Path) -> None:
    subprocess.run(
        cmd, shell=True, check=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"}
    )


def _seed(home: Path, lines: list[str]) -> Path:
    ssh = home / ".ssh"
    ssh.mkdir(parents=True, exist_ok=True)
    ak = ssh / "authorized_keys"
    ak.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
    return ak


# ── pure-Python helpers ───────────────────────────────────────────────────────


def test_key_blob_extracts_second_field():
    assert _key_blob("ssh-ed25519 AAAABLOB runspec-console") == "AAAABLOB"
    assert _key_blob("ssh-ed25519 AAAABLOB") == "AAAABLOB"
    assert _key_blob("") == ""
    assert _key_blob("# a comment line") == ""
    # A key carrying options (field 2 isn't the blob) is not misidentified.
    assert _key_blob('command="x" ssh-ed25519 AAAABLOB') == ""


def test_generated_key_carries_marker(tmp_path):
    b = Bridge.__new__(Bridge)
    res = Bridge._generate_keypair_to_path(b, tmp_path / "k")
    assert res["ok"]
    assert res["public_key"].endswith(f" {_KEY_MARKER}")
    # The persisted .pub carries it too, so later reads identify the key.
    assert (tmp_path / "k.pub").read_text().strip().endswith(_KEY_MARKER)


def _write_priv(path: Path) -> str:
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        .decode()
    )
    return _key_blob(pub)


def test_stale_key_blobs_from_pub_and_priv_backups(tmp_path):
    b = Bridge.__new__(Bridge)
    # A public backup (read directly) and a private backup (derived).
    (tmp_path / "runspec_ed25519.pub.bak.100").write_text(
        "ssh-ed25519 PUBBAKBLOB runspec-console\n"
    )
    priv_blob = _write_priv(tmp_path / "runspec_ed25519.bak.200")
    blobs = Bridge._stale_key_blobs(b, tmp_path, exclude=set())
    assert "PUBBAKBLOB" in blobs
    assert priv_blob in blobs


def test_stale_key_blobs_excludes_current_and_new(tmp_path):
    b = Bridge.__new__(Bridge)
    (tmp_path / "runspec_ed25519.pub.bak.1").write_text("ssh-ed25519 KEEPME x\n")
    assert Bridge._stale_key_blobs(b, tmp_path, exclude={"KEEPME"}) == []


# ── real shell/awk behaviour ──────────────────────────────────────────────────


@needs_sh
def test_push_prunes_marked_keeps_user_and_current(tmp_path):
    user_key = "ssh-rsa AAAAUSERKEY jason@laptop"
    old_key = "ssh-ed25519 OLDBLOB runspec-console"  # current — must survive
    stale_key = "ssh-ed25519 STALEBLOB runspec-console"  # leftover — must go
    new_key = "ssh-ed25519 NEWBLOB runspec-console"
    ak = _seed(tmp_path, [user_key, old_key, stale_key])

    cmd = _build_rotation_push_cmd(new_key, old_blob="OLDBLOB", remove_blobs=[])
    _run(cmd, tmp_path)

    lines = ak.read_text().splitlines()
    assert user_key in lines  # operator's own key untouched
    assert old_key in lines  # current key kept (safety net)
    assert new_key in lines  # new key installed
    assert stale_key not in lines  # stale marked runspec key pruned
    # Exactly one runspec marker line beyond old+new (i.e. the stale one is gone).
    assert sum(1 for ln in lines if _KEY_MARKER in ln) == 2


@needs_sh
def test_push_prunes_unmarked_backup_blob(tmp_path):
    # A pre-marker runspec key (no comment) is pruned via exact blob match.
    user_key = "ssh-ed25519 USERBLOB jason"
    legacy_key = "ssh-ed25519 LEGACYBLOB"  # old runspec key, no marker
    old_key = "ssh-ed25519 OLDBLOB runspec-console"
    new_key = "ssh-ed25519 NEWBLOB runspec-console"
    ak = _seed(tmp_path, [user_key, legacy_key, old_key])

    cmd = _build_rotation_push_cmd(
        new_key, old_blob="OLDBLOB", remove_blobs=["LEGACYBLOB"]
    )
    _run(cmd, tmp_path)

    lines = ak.read_text().splitlines()
    assert user_key in lines  # a real user ed25519 key is preserved
    assert legacy_key not in lines  # historical runspec key pruned by blob
    assert old_key in lines
    assert new_key in lines


@needs_sh
def test_push_is_idempotent(tmp_path):
    old_key = "ssh-ed25519 OLDBLOB runspec-console"
    new_key = "ssh-ed25519 NEWBLOB runspec-console"
    ak = _seed(tmp_path, [old_key])
    cmd = _build_rotation_push_cmd(new_key, old_blob="OLDBLOB", remove_blobs=[])
    _run(cmd, tmp_path)
    _run(cmd, tmp_path)  # second push must not duplicate the new key
    lines = ak.read_text().splitlines()
    assert lines.count(new_key) == 1
    assert old_key in lines


@needs_sh
def test_push_never_empties_file_and_preserves_perms(tmp_path):
    old_key = "ssh-ed25519 OLDBLOB runspec-console"
    new_key = "ssh-ed25519 NEWBLOB runspec-console"
    ak = _seed(tmp_path, [old_key])
    ak.chmod(0o600)
    before_mode = ak.stat().st_mode
    cmd = _build_rotation_push_cmd(new_key, old_blob="OLDBLOB", remove_blobs=[])
    _run(cmd, tmp_path)
    assert ak.read_text().strip() != ""  # never truncated to empty
    assert ak.stat().st_mode == before_mode  # cat > ak preserves the inode/perms
    # No temp file left behind.
    assert not list((tmp_path / ".ssh").glob("ak.*"))

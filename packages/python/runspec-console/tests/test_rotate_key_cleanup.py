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

from runspec_console import config
from runspec_console.bridge import Bridge
from runspec_console.bridge_config_ssh import (
    _KEY_MARKER,
    _build_rotation_push_cmd,
    _key_blob,
)
from runspec_console.bridge_utils import DEFAULT_KEY_MARKER, key_marker

_SH = shutil.which("sh")
_AWK = shutil.which("awk")
needs_sh = pytest.mark.skipif(not (_SH and _AWK), reason="needs sh + awk")


@pytest.fixture(autouse=True)
def _default_install(monkeypatch):
    """Pin the branded slug to the default install so the marker is the historic
    ``runspec-console`` regardless of what's installed in the test env."""
    monkeypatch.setattr(config, "_app_slug", lambda: "runspec-console")


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


# ── white-label marker (follows the package name, like the config folder) ────


def test_marker_follows_white_label_slug(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_app_slug", lambda: "jason-console")
    assert key_marker() == "jason-console"
    b = Bridge.__new__(Bridge)
    res = Bridge._generate_keypair_to_path(b, tmp_path / "k")
    assert res["public_key"].endswith(" jason-console")
    assert (tmp_path / "k.pub").read_text().strip().endswith(" jason-console")
    cmd = _build_rotation_push_cmd("ssh-ed25519 NEW jason-console", "OLD", [])
    assert '$NF=="jason-console"' in cmd
    assert "runspec-console" not in cmd


def test_marker_falls_back_when_slug_unsafe(monkeypatch):
    # A slug that couldn't be interpolated into the single-quoted awk program.
    monkeypatch.setattr(config, "_app_slug", lambda: "acme console'x")
    assert key_marker() == DEFAULT_KEY_MARKER
    cmd = _build_rotation_push_cmd("ssh-ed25519 NEW x", "OLD", [], marker="a b")
    assert f'$NF=="{DEFAULT_KEY_MARKER}"' in cmd


def test_default_marker_unchanged():
    assert _KEY_MARKER == DEFAULT_KEY_MARKER == "runspec-console"
    assert key_marker() == "runspec-console"


@needs_sh
def test_push_default_install_leaves_other_apps_keys(tmp_path):
    stale = "ssh-ed25519 STALEBLOB runspec-console"  # ours — must go
    other_app = "ssh-ed25519 OTHERBLOB jason-console"  # another install's — keep
    corp = "ssh-ed25519 CORPBLOB runspec-console-corp"  # prefix collision — keep
    old_key = "ssh-ed25519 OLDBLOB runspec-console"
    new_key = "ssh-ed25519 NEWBLOB runspec-console"
    ak = _seed(tmp_path, [stale, other_app, corp, old_key])
    _run(
        _build_rotation_push_cmd(new_key, old_blob="OLDBLOB", remove_blobs=[]), tmp_path
    )
    lines = ak.read_text().splitlines()
    assert stale not in lines
    assert other_app in lines
    assert corp in lines
    assert old_key in lines and new_key in lines


@needs_sh
def test_push_white_label_prunes_only_its_own_tag(tmp_path):
    mine = "ssh-ed25519 MINEBLOB jason-console"  # ours — must go
    default_app = "ssh-ed25519 DEFBLOB runspec-console"  # the default install's — keep
    legacy = "ssh-ed25519 LEGACYBLOB runspec-console"  # ours pre-0.265 (in backups)
    old_key = "ssh-ed25519 OLDBLOB jason-console"
    new_key = "ssh-ed25519 NEWBLOB jason-console"
    ak = _seed(tmp_path, [mine, default_app, legacy, old_key])
    cmd = _build_rotation_push_cmd(
        new_key, old_blob="OLDBLOB", remove_blobs=["LEGACYBLOB"], marker="jason-console"
    )
    _run(cmd, tmp_path)
    lines = ak.read_text().splitlines()
    assert mine not in lines  # tagged with our marker
    assert legacy not in lines  # migration: pruned via the backup-blob match
    assert default_app in lines  # the other install's key survives
    assert old_key in lines and new_key in lines


@needs_sh
def test_push_marker_must_be_the_last_field(tmp_path):
    # Exact trailing-field semantics: a line merely *containing* the marker is
    # not ours.
    odd = "ssh-ed25519 ODDBLOB runspec-console extra"
    old_key = "ssh-ed25519 OLDBLOB runspec-console"
    new_key = "ssh-ed25519 NEWBLOB runspec-console"
    ak = _seed(tmp_path, [odd, old_key])
    _run(
        _build_rotation_push_cmd(new_key, old_blob="OLDBLOB", remove_blobs=[]), tmp_path
    )
    assert odd in ak.read_text().splitlines()

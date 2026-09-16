"""generate-ssh-key — generate or rotate the runspec-console SSH key pair."""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import runspec

from ..bridge_utils import key_marker
from ..config import app_dir, read_config, write_config

logger = logging.getLogger(__name__)


def _normalize_path(p: str) -> str:
    return p.replace("\\", "/")


def _generate(rotate: bool) -> dict:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization as _serial
    except ImportError:
        return {
            "ok": False,
            "public_key": "",
            "key_path": "",
            "message": "cryptography package not installed — run: pip install cryptography",
        }

    # The console's config dir (the branded %APPDATA%\<package> folder), not a
    # hardcoded "runspec-console" — so the key lives with the rest of this
    # install's state instead of a stray folder.
    key_dir = app_dir()
    key_dir.mkdir(parents=True, exist_ok=True)
    key_path = key_dir / "runspec_ed25519"

    if rotate and key_path.exists():
        backup = key_dir / f"runspec_ed25519.bak.{int(time.time())}"
        key_path.rename(backup)
        logger.info("Existing key backed up to %s", _normalize_path(str(backup)))

    private_key = Ed25519PrivateKey.generate()

    key_path.write_bytes(
        private_key.private_bytes(
            encoding=_serial.Encoding.PEM,
            format=_serial.PrivateFormat.OpenSSH,
            encryption_algorithm=_serial.NoEncryption(),
        )
    )

    raw_priv = private_key.private_bytes(
        encoding=_serial.Encoding.Raw,
        format=_serial.PrivateFormat.Raw,
        encryption_algorithm=_serial.NoEncryption(),
    )
    raw_pub = private_key.public_key().public_bytes(
        encoding=_serial.Encoding.Raw,
        format=_serial.PublicFormat.Raw,
    )
    _write_ppk_v2(Path(str(key_path) + ".ppk"), raw_priv, raw_pub)

    public_key = (
        private_key.public_key()
        .public_bytes(
            encoding=_serial.Encoding.OpenSSH,
            format=_serial.PublicFormat.OpenSSH,
        )
        .decode()
        .strip()
    )
    # Tag with this install's marker (the white-label package name) and persist
    # the .pub, exactly like the Settings → SSH generator, so a later rotation
    # recognises and prunes this key from hosts too.
    public_key = f"{public_key} {key_marker()}"
    Path(str(key_path) + ".pub").write_text(public_key + "\n", encoding="utf-8")

    normalized = _normalize_path(str(key_path))
    return {
        "ok": True,
        "public_key": public_key,
        "key_path": normalized,
        "message": f"Key written to {normalized}",
    }


def _write_ppk_v2(
    dest: Path, raw_priv: bytes, raw_pub: bytes, comment: str | None = None
) -> None:
    import base64
    import hashlib
    import hmac as _hmac
    import struct
    import textwrap

    key_type = b"ssh-ed25519"
    enc_type = b"none"
    comment = comment or key_marker()
    comment_b = comment.encode()
    pub_blob = (
        struct.pack(">I", len(key_type))
        + key_type
        + struct.pack(">I", len(raw_pub))
        + raw_pub
    )
    priv_blob = (
        struct.pack(">I", len(raw_priv))
        + raw_priv
        + struct.pack(">I", len(raw_pub))
        + raw_pub
    )
    mac_key = hashlib.sha1(b"putty-private-key-file-mac-key").digest()
    mac_data = b"".join(
        struct.pack(">I", len(f)) + f
        for f in (key_type, enc_type, comment_b, pub_blob, priv_blob)
    )
    mac_hex = _hmac.new(mac_key, mac_data, hashlib.sha1).hexdigest()

    def b64lines(data: bytes) -> tuple[int, str]:
        lines = textwrap.wrap(base64.b64encode(data).decode(), 64)
        return len(lines), "\n".join(lines)

    pub_n, pub_s = b64lines(pub_blob)
    priv_n, priv_s = b64lines(priv_blob)
    dest.write_text(
        "\n".join(
            [
                "PuTTY-User-Key-File-2: ssh-ed25519",
                "Encryption: none",
                f"Comment: {comment}",
                f"Public-Lines: {pub_n}",
                pub_s,
                f"Private-Lines: {priv_n}",
                priv_s,
                f"Private-MAC: {mac_hex}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    args = runspec.parse("generate-ssh-key")
    rotate = bool(args.rotate.value)

    action = "Rotating" if rotate else "Generating"
    logger.info("%s SSH key …", action)

    result = _generate(rotate)

    if not result["ok"]:
        logger.error("%s", result["message"])
        sys.exit(1)

    # Record creation timestamp so the Settings UI can track key age
    cfg = read_config()
    ssh = dict(cfg.get("ssh", {}))
    ssh["identityFile"] = result["key_path"]
    ssh["key_created_at"] = datetime.now(timezone.utc).isoformat()
    cfg["ssh"] = ssh
    write_config(cfg)

    logger.info("%s", result["message"])
    logger.info("Public key:\n%s", result["public_key"])
    logger.info("Add the public key to ~/.ssh/authorized_keys on each remote host.")

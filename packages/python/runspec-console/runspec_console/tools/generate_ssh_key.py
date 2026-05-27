"""generate-ssh-key — generate or rotate the runspec-console SSH key pair."""

from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import runspec

from ..config import read_config, write_config

logger = logging.getLogger(__name__)


def _run_keygen(key_path: str) -> dict[str, Any]:
    """Run ssh-keygen; back up any existing key first. Returns {ok, public_key, message}."""
    path = Path((key_path or "~/.ssh/runspec_ed25519").strip()).expanduser()
    pub_path = Path(str(path) + ".pub")

    if path.exists():
        ts = datetime.now().strftime("%Y%m%d")
        path.rename(Path(str(path) + f".bak.{ts}"))
        if pub_path.exists():
            pub_path.rename(Path(str(pub_path) + f".bak.{ts}"))

    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(path), "-N", "", "-C", "runspec-console"],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return {"ok": False, "public_key": "", "message": "ssh-keygen not found — install OpenSSH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "public_key": "", "message": "ssh-keygen timed out"}

    if result.returncode != 0:
        return {"ok": False, "public_key": "", "message": result.stderr.strip() or "ssh-keygen failed"}

    pub_key = pub_path.read_text(encoding="utf-8").strip() if pub_path.exists() else ""
    return {"ok": True, "public_key": pub_key, "message": f"Key generated at {path}"}


def main() -> None:
    args = runspec.parse("generate-ssh-key")
    key_path = str(args.key_path.value).strip() if args.key_path.value else "~/.ssh/runspec_ed25519"

    logger.info("Generating SSH key at %s …", key_path)
    result = _run_keygen(key_path)

    if not result["ok"]:
        logger.error("%s", result["message"])
        sys.exit(1)

    # Record creation timestamp so the Settings UI can track key age
    cfg = read_config()
    ssh = dict(cfg.get("ssh", {}))
    ssh["key_created_at"] = datetime.now(timezone.utc).isoformat()
    cfg["ssh"] = ssh
    write_config(cfg)

    logger.info("%s", result["message"])
    logger.info("Public key:\n%s", result["public_key"])
    logger.info("Add the public key to ~/.ssh/authorized_keys on each remote host.")

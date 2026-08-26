"""
sshkeys.py — the gateway's own managed SSH keypair.

So a user never has to touch ``~/.ssh`` to reach their fleet, runspec-mcp manages
its **own** ed25519 keypair in its app dir and uses it as the default SSH identity
for every host (a per-host ``identity_file`` still overrides, and the ssh-agent /
default ``~/.ssh/id_*`` keys remain a fallback). ``runspec-mcp copy-key`` installs
the public half on hosts over a one-time password connection.

The private key never leaves the app dir; the public key carries a stable
``runspec-mcp`` comment marker (ignored by sshd for auth) so it is recognisable in
a host's ``authorized_keys``.
"""

from __future__ import annotations

import stat
from pathlib import Path

from .config import FleetConfig, app_dir

# Comment appended to the public key so it's identifiable in authorized_keys.
KEY_MARKER = "runspec-mcp"

_PRIVATE_NAME = "id_runspec_mcp"


def managed_key_path() -> Path:
    """Path to the managed private key (``.pub`` sits alongside)."""
    return app_dir() / _PRIVATE_NAME


def _public_path() -> Path:
    return Path(str(managed_key_path()) + ".pub")


def ensure_managed_key() -> Path:
    """Generate the managed ed25519 keypair if absent; return the private-key path.

    Idempotent: an existing key is left untouched. The private key is written
    OpenSSH-PEM with ``0600`` perms; the public key gets the ``runspec-mcp`` marker
    comment."""
    priv = managed_key_path()
    if priv.exists():
        return priv

    from cryptography.hazmat.primitives import serialization as _serial
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()

    priv.write_bytes(
        key.private_bytes(
            encoding=_serial.Encoding.PEM,
            format=_serial.PrivateFormat.OpenSSH,
            encryption_algorithm=_serial.NoEncryption(),
        )
    )
    try:
        priv.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass  # best-effort on platforms without POSIX perms

    pub = key.public_key().public_bytes(encoding=_serial.Encoding.OpenSSH, format=_serial.PublicFormat.OpenSSH).decode().strip()
    if KEY_MARKER not in pub:
        pub = f"{pub} {KEY_MARKER}"
    _public_path().write_text(pub + "\n", encoding="utf-8")
    return priv


def managed_public_key() -> str:
    """The managed public key line (generating the keypair if needed)."""
    ensure_managed_key()
    return _public_path().read_text(encoding="utf-8").strip()


def apply_managed_identity(cfg: FleetConfig) -> None:
    """Default the SSH identity to the managed key when the operator set none.

    Mutates ``cfg.ssh`` in place: if neither a global ``[ssh] identityFile`` nor
    per-host ``identity_file`` is the chosen identity, point the global identity at
    the managed key (generating it on first use). A per-host ``identity_file`` still
    wins in ``executor`` (it's tried before the global one), and the ssh-agent /
    default keys remain a fallback.

    No-op when the operator set a global identity, or when the fleet has no remote
    hosts (a purely-local fleet never opens SSH, so no key is generated)."""
    if str(cfg.ssh.get("identityFile") or "").strip():
        return
    if not any(str(h.get("ssh") or "").strip() for h in cfg.hosts):
        return
    cfg.ssh["identityFile"] = str(ensure_managed_key())


def authorize_command(public_key: str) -> str:
    """Idempotent remote shell command to append ``public_key`` to authorized_keys.

    Creates ``~/.ssh`` (0700) and ``authorized_keys`` (0600) if missing and appends
    the key only when not already present. The key is a single-quoted literal;
    OpenSSH public keys contain no single quotes, so this is injection-safe."""
    key = public_key.strip()
    return (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        f"(grep -qF '{key}' ~/.ssh/authorized_keys || printf '%s\\n' '{key}' >> ~/.ssh/authorized_keys)"
    )


def copy_key_to_host(ssh_target: str, password: str, cfg: FleetConfig, identity_file: str | None = None) -> tuple[int, str]:
    """Install the managed public key on one host over a password connection.

    Returns ``(exit_code, message)``. Uses paramiko password auth (cross-platform,
    no ``sshpass`` needed)."""
    from . import executor

    pub = managed_public_key()
    cmd = authorize_command(pub)
    try:
        client = executor._make_ssh_client(ssh_target, identity_file, cfg.ssh, password=password)
    except Exception as exc:  # connect/auth failure
        return -1, f"connection failed: {executor.friendly_ssh_error(exc)}"
    try:
        _stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
        code = stdout.channel.recv_exit_status()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        if code == 0:
            return 0, "public key installed"
        return code, err or f"remote command exited {code}"
    except Exception as exc:
        return -1, str(exc)
    finally:
        try:
            client.close()
        except Exception:
            pass

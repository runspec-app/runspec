import subprocess
import sys
from pathlib import Path

import tomllib

import runspec as rs
from runspec_chat.chat import _resolve_hosts_path


def _load_hosts(hosts_path: Path) -> dict:
    hosts_path = _resolve_hosts_path(hosts_path)
    if not hosts_path.exists():
        print(f"No hosts config found at {hosts_path}")
        print("Copy jump_hosts.toml.example and edit it, then re-run setup-keys.")
        sys.exit(1)
    with open(hosts_path, "rb") as f:
        return tomllib.load(f)


def _ensure_key(key_path: Path, key_type: str) -> Path:
    pub = key_path.with_suffix(".pub")
    if key_path.exists():
        print(f"Using existing key: {key_path}")
        return pub

    print(f"Generating {key_type} key at {key_path} ...")
    result = subprocess.run(
        [
            "ssh-keygen",
            "-t",
            key_type,
            "-f",
            str(key_path),
            "-N",
            "",
            "-C",
            "runspec-chat",
        ]
    )
    if result.returncode != 0:
        print("ssh-keygen failed.")
        sys.exit(1)
    print(f"Key created: {pub}")
    return pub


def _copy_to_host(pub_key: Path, ssh_target: str, name: str) -> bool:
    print(f"\nCopying public key to {name} ({ssh_target}) ...")
    print("You may be prompted for the remote password.")
    result = subprocess.run(["ssh-copy-id", "-i", str(pub_key), ssh_target])
    if result.returncode != 0:
        print(f"  Warning: ssh-copy-id failed for {name} — skipping.")
        return False
    print("  Done.")
    return True


def main() -> None:
    spec = rs.parse("setup-keys")

    hosts_path = Path(str(spec.hosts)).expanduser()
    config = _load_hosts(hosts_path)

    ssh_hosts = [
        (name, info["ssh"])
        for name, info in config.get("hosts", {}).items()
        if info.get("ssh")
    ]

    if not ssh_hosts:
        print("No SSH hosts found in the config (hosts with an 'ssh' field).")
        sys.exit(0)

    key_type = str(spec.key_type)
    key_path = Path.home() / ".ssh" / f"runspec-chat_{key_type}"
    pub_key = _ensure_key(key_path, key_type)

    ok, failed = [], []
    for name, ssh_target in ssh_hosts:
        if _copy_to_host(pub_key, ssh_target, name):
            ok.append(name)
        else:
            failed.append(name)

    print(f"\nDone. {len(ok)} host(s) configured", end="")
    if failed:
        print(f", {len(failed)} failed: {', '.join(failed)}", end="")
    print(".")

    if ok:
        print("\nAdd this to your ~/.ssh/config for each host:")
        print(f"  IdentityFile ~/.ssh/runspec-chat_{key_type}")

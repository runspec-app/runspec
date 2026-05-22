import json
import os
import re
import subprocess
import sys
from pathlib import Path

import runspec as rs

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


def _shared_pass_key() -> str:
    return "SSH_PASS"


def _host_pass_key(host_name: str) -> str:
    return f"SSH_{host_name.upper().replace('-', '_').replace(' ', '_')}_PASS"


def _sync_user_env(hosts_path: Path, chainlit_config: Path) -> None:
    """Rewrite user_env in .chainlit/config.toml from hosts.toml.

    Hosts that share the default username → one SSH_PASS field.
    Hosts that declare their own username → individual SSH_{HOST}_PASS fields.
    """
    user_env = ["ANTHROPIC_API_KEY"]

    if hosts_path.exists():
        with open(hosts_path, "rb") as f:
            cfg = tomllib.load(f)

        has_shared = False
        host_keys: list[str] = []
        for name, info in cfg.get("hosts", {}).items():
            if not info.get("ssh"):
                continue
            if info.get("user"):
                host_keys.append(_host_pass_key(name))
            else:
                has_shared = True

        if has_shared:
            user_env.append(_shared_pass_key())
        user_env.extend(host_keys)

    if not chainlit_config.exists():
        return

    text = chainlit_config.read_text()
    text = re.sub(r"user_env = \[.*?\]", f"user_env = {json.dumps(user_env)}", text)
    chainlit_config.write_text(text)


def main() -> None:
    spec = rs.parse("runspec-chat")

    package_root = Path(__file__).parent.parent
    os.environ["CHAINLIT_ROOT"] = str(package_root)

    hosts_path = Path(str(spec.hosts)).expanduser()
    chainlit_config = package_root / ".chainlit" / "config.toml"
    _sync_user_env(hosts_path, chainlit_config)

    if spec.model:
        os.environ["RUNSPEC_CHAT_MODEL"] = str(spec.model)
    if spec.hosts:
        os.environ["RUNSPEC_CHAT_HOSTS"] = str(spec.hosts)

    app_py = Path(__file__).parent / "app.py"
    cmd = [
        sys.executable, "-m", "chainlit", "run", str(app_py),
        "--port", str(spec.port),
        "--host", "0.0.0.0",
    ]
    try:
        sys.exit(subprocess.run(cmd).returncode)
    except KeyboardInterrupt:
        sys.exit(0)

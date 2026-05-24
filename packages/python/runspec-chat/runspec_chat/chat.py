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


def _resolve_hosts_path(path: Path) -> Path:
    if not path.exists():
        legacy = path.parent / "hosts.toml"
        if legacy.exists():
            print(
                f"[runspec-chat] {legacy} is deprecated; rename to {path.name} to suppress this warning.",
                file=sys.stderr,
            )
            return legacy
    return path


def _sync_user_env(hosts_path: Path, chainlit_config: Path) -> None:
    hosts_path = _resolve_hosts_path(hosts_path)
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

    package_root = Path(__file__).parent
    os.environ["CHAINLIT_ROOT"] = str(package_root)

    hosts_path = Path(str(spec.hosts)).expanduser()
    chainlit_config = package_root / ".chainlit" / "config.toml"
    _sync_user_env(hosts_path, chainlit_config)

    if spec.model:
        os.environ["RUNSPEC_CHAT_MODEL"] = str(spec.model)
    if spec.hosts:
        os.environ["RUNSPEC_CHAT_HOSTS"] = str(spec.hosts)

    app_py = Path(__file__).parent / "app.py"
    # Use our launcher instead of `python -m chainlit` so the Python 3.14
    # nest_asyncio compatibility shim is applied before chainlit's CLI runs.
    cmd = [
        sys.executable, "-m", "runspec_chat._chainlit_launcher",
        "run", str(app_py),
        "--port", str(spec.port),
        "--host", str(spec.host),
    ]
    if spec.watch:
        cmd.append("--watch")
    if spec.headless:
        cmd.append("--headless")
    if spec.root_path:
        cmd += ["--root-path", str(spec.root_path)]
    if spec.ssl_cert:
        cmd += ["--ssl-cert", str(spec.ssl_cert)]
    if spec.ssl_key:
        cmd += ["--ssl-key", str(spec.ssl_key)]
    try:
        sys.exit(subprocess.run(cmd).returncode)
    except KeyboardInterrupt:
        sys.exit(0)

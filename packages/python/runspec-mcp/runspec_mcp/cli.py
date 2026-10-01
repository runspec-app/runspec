"""
cli.py — the ``runspec-mcp`` command line.

    runspec-mcp serve                 # the MCP gateway (stdio) — what the plugin runs
    runspec-mcp hosts                 # list configured hosts + probe connectivity
    runspec-mcp discover [--host H]   # print discovered tools (debug)
    runspec-mcp cred set|list|rm ID   # manage credential secrets in the OS keychain
    runspec-mcp key [--show|--regenerate]  # the gateway's managed SSH public key
    runspec-mcp copy-key [--all] [HOST...] # install the managed key on host(s)
    runspec-mcp seed-status           # show any bundled config-seed contributors
    runspec-mcp plugin-path           # print the bundled Claude Code plugin source dir
    runspec-mcp claude-plugin         # render the plugin for this install, print its path
    runspec-mcp check                 # validate config + connectivity + manifest
    runspec-mcp setup claude|vscode|json  # connect an MCP client (absolute paths)
"""

from __future__ import annotations

import argparse
import getpass
import json
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

from . import __version__
from . import credentials as cred
from .config import FleetConfig, app_dir, default_config_path, load_config, manifest_path


def _load(args: argparse.Namespace) -> FleetConfig:
    config_path = Path(args.config).expanduser() if getattr(args, "config", None) else None
    config_dir = Path(args.config_dir).expanduser() if getattr(args, "config_dir", None) else None
    return load_config(config_path=config_path, config_dir=config_dir)


def _add_config_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="path to runspec_mcp.toml (default: $RUNSPEC_MCP_CONFIG / ./runspec_mcp.toml)")
    p.add_argument("--config-dir", help="a runspec-console config directory to reuse (hosts + credentials + config.toml; default: $RUNSPEC_MCP_CONFIG_DIR)")


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(_load(args))
    return 0


def cmd_hosts(args: argparse.Namespace) -> int:
    from .catalog import Catalog
    from .server import build_pool

    cfg = _load(args)
    catalog = Catalog(cfg, build_pool(cfg))
    catalog.refresh()
    for s in catalog.status():
        mark = "✓" if s.connected else "✗"
        loc = s.ssh or "(local)"
        extra = f" — {s.error}" if s.error else ""
        print(f"{mark}  {s.name:20} {loc:30} {s.venvs} venv(s), {s.tools} tool(s){extra}")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    from .catalog import Catalog
    from .server import build_pool

    cfg = _load(args)
    catalog = Catalog(cfg, build_pool(cfg))
    catalog.refresh()
    for t in catalog.tools():
        if args.host and str(t.host.get("name") or "") != args.host:
            continue
        print(f"{t.name:40} [{t.autonomy}] {t.host.get('name')}::{t.group}")
    return 0


def cmd_cred(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if args.action == "list":
        backend = cred.backend_name() or "(no keychain backend)"
        print(f"keychain backend: {backend}")
        for c in cfg.credentials:
            cid = str(c.get("id", ""))
            has = "●" if cred.has_secret(cid) else "○"
            names = ", ".join(cred.env_var_names(str(c.get("label", "")), str(c.get("kind", "userpass")))) or "(no env vars)"
            print(f"  {has} {cid:16} {c.get('kind', 'userpass'):10} → {names}")
        return 0
    if args.action == "set":
        if not cred.available():
            print(f"✗  {_no_keychain_reason()}", file=sys.stderr)
            return 1
        secret = getpass.getpass(f"secret for '{args.id}': ")
        cred.set_secret(args.id, secret)
        print(f"✓  stored secret for '{args.id}' in the {cred.backend_name()} keychain")
        return 0
    if args.action == "rm":
        cred.delete_secret(args.id)
        print(f"✓  removed secret for '{args.id}'")
        return 0
    return 2


def cmd_key(args: argparse.Namespace) -> int:
    from . import sshkeys

    if args.regenerate:
        priv = sshkeys.managed_key_path()
        for f in (priv, Path(str(priv) + ".pub")):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
        print(f"regenerated managed key at {priv}", file=sys.stderr)
    print(sshkeys.managed_public_key())
    return 0


def cmd_copy_key(args: argparse.Namespace) -> int:
    import getpass

    from . import sshkeys

    cfg = _load(args)
    remote_hosts = [h for h in cfg.hosts if str(h.get("ssh") or "").strip()]
    if args.all:
        targets = remote_hosts
    elif args.hosts:
        wanted = set(args.hosts)
        targets = [h for h in remote_hosts if str(h.get("name") or "") in wanted]
    else:
        print("✗  specify one or more host names, or --all", file=sys.stderr)
        return 2
    if not targets:
        print("✗  no matching remote hosts in the config", file=sys.stderr)
        return 1

    print(f"managed public key:\n  {sshkeys.managed_public_key()}\n")
    password = getpass.getpass("host password (used for every selected host): ")
    rc = 0
    for h in targets:
        code, msg = sshkeys.copy_key_to_host(str(h["ssh"]), password, cfg)
        print(f"{'✓' if code == 0 else '✗'}  {str(h.get('name') or h['ssh']):20} {msg}")
        if code != 0:
            rc = 1
    return rc


def cmd_seed_status(args: argparse.Namespace) -> int:
    from . import configseed

    fetched, stamp = configseed.discover()
    if not stamp:
        print("no config-seed packages installed.")
        return 0
    print("config-seed contributors:")
    for dist, version in sorted(stamp.items()):
        print(f"  {dist} {version}")
    print("files:", ", ".join(sorted(fetched)))
    return 0


def _no_keychain_reason() -> str:
    """Why no secret can be stored, and what to do — the extra missing is only
    one cause; on Linux the usual one is no Secret Service running/unlocked."""
    if cred._keyring() is None:
        return "no keychain support installed — pip install 'runspec-mcp[credentials]'"
    if sys.platform.startswith("linux"):
        return (
            "no OS keychain is available. On Linux this needs a Secret Service provider "
            "(GNOME Keyring or KWallet) running and unlocked in your session — typically "
            "a desktop login; over SSH or on a headless server there is none. Store the "
            "secret from a desktop session, or install a file-based keyring backend "
            "(e.g. pip install keyrings.cryptfile) and select it with PYTHON_KEYRING_BACKEND."
        )
    return "no OS keychain is available to the keyring library in this session"


def _gateway_bin() -> str:
    """Absolute path of this install's runspec-mcp entry point. MCP clients spawn
    servers with their own PATH, which usually doesn't include a venv's bin dir —
    so a bare ``runspec-mcp`` command silently fails to start."""
    name = "runspec-mcp.exe" if sys.platform == "win32" else "runspec-mcp"
    candidate = Path(sysconfig.get_path("scripts")) / name
    if candidate.exists():
        return str(candidate)
    found = shutil.which("runspec-mcp")
    return str(Path(found).resolve()) if found else str(Path(sys.argv[0]).resolve())


def _config_for_setup(args: argparse.Namespace) -> Path:
    if getattr(args, "config", None):
        return Path(args.config).expanduser().resolve()
    found = default_config_path()
    return found.resolve() if found else app_dir() / "runspec_mcp.toml"


def _setup_env(args: argparse.Namespace) -> tuple[str, str]:
    """The (variable, value) the client starts the gateway with: a console config
    directory (``--config-dir``) or a ``runspec_mcp.toml`` (``--config``)."""
    if getattr(args, "config_dir", None):
        config_dir = Path(args.config_dir).expanduser().resolve()
        if not (config_dir / "runspec_hosts.toml").is_file():
            print(f"note: no runspec_hosts.toml in {config_dir} — is it a runspec-console config directory?", file=sys.stderr)
        return "RUNSPEC_MCP_CONFIG_DIR", str(config_dir)
    config = _config_for_setup(args)
    if not config.is_file():
        print(f"note: no config at {config} yet — create it (see docs/mcp.md), then re-run.", file=sys.stderr)
    return "RUNSPEC_MCP_CONFIG", str(config)


def cmd_setup(args: argparse.Namespace) -> int:
    """Print (or apply) the exact client config — absolute binary + config paths,
    so the client can start the gateway whatever its PATH and working dir."""
    binary = _gateway_bin()
    var, value = _setup_env(args)
    server = {"type": "stdio", "command": binary, "args": ["serve"], "env": {var: value}}
    if args.client == "claude":
        cmd = ["claude", "mcp", "add", "runspec", "--scope", "user", "--env", f"{var}={value}", "--", binary, "serve"]
        if args.apply:
            if shutil.which("claude") is None:
                print("✗  the claude CLI isn't on PATH — run the command below yourself.", file=sys.stderr)
                print(" ".join(cmd))
                return 1
            return subprocess.run(cmd).returncode
        print("# Recommended: the Claude Code plugin (adds the autonomy hook, so autonomous")
        print("# tools run without prompting). In Claude Code:")
        print("#   /plugin marketplace add https://runspec.app/marketplace-pip.json")
        print("#   /plugin install runspec-mcp@runspec-pip")
        print("# and when it asks, set the config to:")
        print(f"#   {'runspec-console config folder' if var == 'RUNSPEC_MCP_CONFIG_DIR' else 'runspec_mcp.toml'}: {value}")
        key = "config_dir" if var == "RUNSPEC_MCP_CONFIG_DIR" else "config_file"
        print("# or, from a shell:")
        print("#   claude plugin marketplace add https://runspec.app/marketplace-pip.json")
        print(f"#   claude plugin install runspec-mcp@runspec-pip --config {key}={value}")
        print("# (No uv? This uses this install. With uv, runspec.app/marketplace.json needs no pip install.)")
        print()
        print("# Or add the gateway alone (no hook — every tool call asks):")
        print(" ".join(cmd))
        print()
        print("# Or try the plugin for one session without installing it:")
        print(f"{var}={value} claude --plugin-dir {cmd_claude_plugin_dir()}")
        return 0
    if args.client == "vscode":
        print("// VS Code / GitHub Copilot: .vscode/mcp.json in a workspace, or the")
        print("// 'MCP: Open User Configuration' command for all workspaces.")
        print(json.dumps({"servers": {"runspec": server}}, indent=2))
        return 0
    print(json.dumps({"mcpServers": {"runspec": {k: v for k, v in server.items() if k != "type"}}}, indent=2))
    return 0


def cmd_claude_plugin_dir() -> Path:
    """Render the plugin for *this* install (absolute gateway binary + Python) into
    the app dir, once per version, and return its path. Re-rendered only when the
    install changed, so a once-per-session call from Claude Code is cheap."""
    from . import claudeplugin

    dest = app_dir() / "claude-plugin" / __version__
    server = [_gateway_bin(), "serve"]
    stamp = json.dumps({"server": server, "python": sys.executable})
    stamp_file = dest / ".rendered-for"
    if not (stamp_file.is_file() and stamp_file.read_text(encoding="utf-8") == stamp):
        claudeplugin.render(dest, server=server, hook_python=sys.executable)
        stamp_file.write_text(stamp, encoding="utf-8")
    return dest


def cmd_claude_plugin(args: argparse.Namespace) -> int:
    """Print the path of a Claude Code plugin wired to this install — what the
    ``runspec-pip`` marketplace's ``command`` source runs."""
    print(cmd_claude_plugin_dir())
    return 0


def cmd_plugin_path(args: argparse.Namespace) -> int:
    plugin_dir = Path(__file__).resolve().parent / "plugin"
    print(plugin_dir)
    return 0 if plugin_dir.is_dir() else 1


def cmd_check(args: argparse.Namespace) -> int:
    from .catalog import Catalog
    from .server import build_pool

    cfg = _load(args)
    print(f"config source: {cfg.source}")
    print(f"hosts: {len(cfg.hosts)}   credentials: {len(cfg.credentials)}   local globs: {cfg.local_venv_globs or '(none)'}")
    catalog = Catalog(cfg, build_pool(cfg))
    catalog.refresh()
    ok = True
    for s in catalog.status():
        if not s.connected:
            ok = False
        print(f"  {'✓' if s.connected else '✗'} {s.name}: {s.tools} tool(s){' — ' + s.error if s.error else ''}")
    print(f"manifest path: {manifest_path()}")
    print(f"total tools: {len(catalog.tools())}")
    if cfg.credentials and not cred.available():
        print(f"  ! credentials: {_no_keychain_reason()}")
    if shutil.which("runspec-mcp") is None:
        print(f"  ! runspec-mcp isn't on PATH, so an MCP client can't start it by name. Use the absolute path {_gateway_bin()} — `runspec-mcp setup claude|vscode` prints the exact config.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="runspec-mcp", description="Connect Claude Code to local and remote runspec runnables.")
    ap.add_argument("--version", action="version", version=f"runspec-mcp {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the MCP gateway (stdio)")
    _add_config_args(p_serve)
    p_serve.set_defaults(func=cmd_serve)

    p_hosts = sub.add_parser("hosts", help="list hosts + connectivity")
    _add_config_args(p_hosts)
    p_hosts.set_defaults(func=cmd_hosts)

    p_disc = sub.add_parser("discover", help="print discovered tools")
    _add_config_args(p_disc)
    p_disc.add_argument("--host", help="only this host")
    p_disc.set_defaults(func=cmd_discover)

    p_cred = sub.add_parser("cred", help="manage credential secrets in the OS keychain")
    _add_config_args(p_cred)
    p_cred.add_argument("action", choices=["set", "list", "rm"])
    p_cred.add_argument("id", nargs="?", default="")
    p_cred.set_defaults(func=cmd_cred)

    p_key = sub.add_parser("key", help="show or regenerate the gateway's managed SSH key")
    p_key.add_argument("--show", action="store_true", help="print the managed public key (default)")
    p_key.add_argument("--regenerate", action="store_true", help="delete and regenerate the managed keypair")
    p_key.set_defaults(func=cmd_key)

    p_copy = sub.add_parser("copy-key", help="install the managed public key on host(s) over a password login")
    _add_config_args(p_copy)
    p_copy.add_argument("hosts", nargs="*", help="host names from the config (or use --all)")
    p_copy.add_argument("--all", action="store_true", help="all configured remote hosts")
    p_copy.set_defaults(func=cmd_copy_key)

    p_seed = sub.add_parser("seed-status", help="show bundled config-seed contributors")
    _add_config_args(p_seed)
    p_seed.set_defaults(func=cmd_seed_status)

    p_pp = sub.add_parser("plugin-path", help="print the bundled Claude Code plugin source directory")
    p_pp.set_defaults(func=cmd_plugin_path)

    p_cp = sub.add_parser("claude-plugin", help="render the Claude Code plugin for this install and print its path")
    p_cp.set_defaults(func=cmd_claude_plugin)

    p_setup = sub.add_parser("setup", help="print the exact MCP client config (claude | vscode | json)")
    p_setup.add_argument("client", choices=["claude", "vscode", "json"])
    p_setup.add_argument("--config", help="the runspec_mcp.toml to point the client at (default: the one the gateway would find)")
    p_setup.add_argument("--config-dir", help="point the client at an existing runspec-console config directory instead")
    p_setup.add_argument("--apply", action="store_true", help="claude only: run `claude mcp add` instead of printing it")
    p_setup.set_defaults(func=cmd_setup)

    p_check = sub.add_parser("check", help="validate config + connectivity + manifest")
    _add_config_args(p_check)
    p_check.set_defaults(func=cmd_check)

    args = ap.parse_args(argv)
    if getattr(args, "action", None) == "set" and not args.id:
        ap.error("cred set requires an ID")
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())

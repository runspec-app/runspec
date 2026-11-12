"""
claude_plugin.py — a Claude Code plugin for the console's own MCP server.

The console writes a one-plugin **local marketplace** into its app dir whenever
its MCP server starts (``{app_dir}/claude-plugin``). Claude Code adds it with
``claude plugin marketplace add <dir>`` and installs the plugin from it. A
marketplace added from a local directory is loaded in place, so a port or token
change reaches Claude Code on its next start without reinstalling.

The plugin carries two things:

* the console's MCP server (``http`` to the live localhost URL). When the server
  has a bearer token the plugin asks for it once at install (``userConfig``,
  ``sensitive`` — Claude Code keeps it in the OS credential store), so the token
  is never written into the plugin files.
* a ``runspec-operator`` agent whose tools are **only** the console's: its MCP
  tools plus the two resource readers it needs for the console's live context.
  Run a whole session as it with ``claude --agent <plugin>:runspec-operator``,
  or let Claude Code hand it ops tasks.

Claude Desktop can't add a local directory: its **Upload plugin** takes a
``.zip``. So the plugin is also written as ``{dest}/<name>.zip`` (the plugin at
the zip root). An uploaded copy doesn't follow the console, so a port or token
change means uploading the new zip. Desktop's *chat* doesn't take plugins at
all; its ``claude_desktop_config.json`` runs local servers over stdio only, so
:func:`desktop_config` builds an entry that reaches the console's HTTP server
through the ``mcp-remote`` bridge (needs Node's ``npx``).

Pure stdlib; the Bridge decides where and when to render.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

SERVER_KEY = "console"
AGENT_NAME = "runspec-operator"
DIR_NAME = "claude-plugin"
# Fixed zip timestamps: the same plugin always zips to the same bytes.
_ZIP_EPOCH = (2020, 1, 1, 0, 0, 0)


def plugin_name(slug: str) -> str:
    """A valid plugin/marketplace name from the install's app slug: letters,
    digits, ``.``, ``_`` and ``-``, starting with a letter or digit."""
    name = "".join(c if c.isalnum() or c in "._-" else "-" for c in slug.lower())
    name = name.strip("-._")
    return name or "runspec-console"


def tool_prefix(name: str) -> str:
    """The prefix Claude Code gives this plugin's MCP tools."""
    return f"mcp__plugin_{name}_{SERVER_KEY}__"


def _agent(name: str, brand: str) -> str:
    tools = f"{tool_prefix(name)}*, ListMcpResourcesTool, ReadMcpResourceTool"
    return f"""---
name: {AGENT_NAME}
description: Operates the {brand} fleet through its runnables. Use for operations work on the operator's machines and SSH hosts — checks, diagnostics, log and file work, restarts, and following the team's runbooks.
tools: {tools}
---

You work through {brand}: its MCP server runs the operator's runspec runnables on
their own machine and their SSH fleet. Those tools are the only ones you have.

Before your first step, read two resources from the `{SERVER_KEY}` server:

- `runspec://system-prompt` — the console assistant's instructions. Follow them.
- `runspec://context` — today's date and timezone, working hours, schedules,
  host groups and the team's pinned facts.

Every run is checked against the runnable's autonomy level. Autonomous runs at
once; confirm and supervised wait for the operator to press Approve in the
console; manual is refused. When a result says it is awaiting approval, say so,
then call the same tool again with the same arguments once the operator has
approved. A decline is the operator's decision: don't look for another way to do
the same thing.

When a required value is unclear and getting it wrong would matter (which host,
sending, scheduling, anything destructive), stop and ask instead of guessing.
"""


def _zip_plugin(src: Path, dest: Path) -> None:
    """Zip ``src`` with its contents at the zip root (``.claude-plugin/…``)."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in src.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(path.relative_to(src).as_posix(), _ZIP_EPOCH)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())


def desktop_config(name: str, url: str, token_required: bool) -> dict[str, Any]:
    """A ``claude_desktop_config.json`` ``mcpServers`` entry for this console.

    Desktop starts local servers as commands (stdio), so ``mcp-remote`` relays
    to the HTTP endpoint. The token rides an env var because Desktop on Windows
    mangles spaces inside ``args``; the placeholder is never the real token."""
    args = ["-y", "mcp-remote", url, "--allow-http"]
    entry: dict[str, Any] = {"command": "npx", "args": args}
    if token_required:
        args += ["--header", "Authorization:${AUTH_HEADER}"]
        entry["env"] = {"AUTH_HEADER": "Bearer <your token>"}
    return {"mcpServers": {name: entry}}


def render(
    dest: Path,
    *,
    slug: str,
    brand: str,
    url: str,
    token_required: bool,
    version: str,
) -> dict[str, Any]:
    """Write the marketplace + plugin into ``dest`` (replacing what's there)
    and return what the Settings tab shows: the folder, the plugin and
    marketplace names, the agent's full name, the install commands, the
    Claude Desktop zip and a ``claude_desktop_config.json`` entry."""
    dest = Path(dest)
    name = plugin_name(slug)
    if dest.exists():
        shutil.rmtree(dest)
    plugin_dir = dest / "plugin"
    (dest / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / "agents").mkdir()

    server: dict[str, Any] = {"type": "http", "url": url}
    manifest: dict[str, Any] = {
        "name": name,
        "displayName": brand,
        "version": version,
        "description": (
            f"{brand}'s runnables and runbooks in Claude Code, gated by the "
            "console's approvals, plus a runspec-operator agent."
        ),
        "author": {"name": brand},
        "mcpServers": {SERVER_KEY: server},
    }
    if token_required:
        server["headers"] = {"Authorization": "Bearer ${user_config.token}"}
        manifest["userConfig"] = {
            "token": {
                "type": "string",
                "title": f"{brand} MCP server token",
                "description": (
                    "The bearer token from the console's Settings → MCP Server."
                ),
                "sensitive": True,
                "required": True,
            }
        }
    marketplace = {
        "name": name,
        "owner": {"name": brand},
        "description": f"The Claude Code plugin for {brand} on this machine.",
        "plugins": [
            {
                "name": name,
                "source": "./plugin",
                "description": manifest["description"],
            }
        ],
    }
    (dest / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(marketplace, indent=2) + "\n", encoding="utf-8"
    )
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (plugin_dir / "agents" / f"{AGENT_NAME}.md").write_text(
        _agent(name, brand), encoding="utf-8"
    )
    zip_path = dest / f"{name}.zip"
    _zip_plugin(plugin_dir, zip_path)
    return {
        "path": str(dest),
        "plugin": name,
        "marketplace": name,
        "agent": f"{name}:{AGENT_NAME}",
        "tool_prefix": tool_prefix(name),
        "token_required": token_required,
        "zip": str(zip_path),
        "desktop_config": json.dumps(
            desktop_config(name, url, token_required), indent=2
        ),
        "commands": [
            f'claude plugin marketplace add "{dest}"',
            f"claude plugin install {name}@{name}",
        ],
    }

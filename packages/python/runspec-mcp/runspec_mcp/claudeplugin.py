"""Render the bundled Claude Code plugin for one of its install routes.

The plugin source ships as package data (``runspec_mcp/plugin``). It is never
installed as-is: each route renders a copy whose MCP server command (and hook
interpreter) fit how the gateway is installed, then Claude Code loads that copy.

Two routes, published as two marketplaces on the docs site (see ``docs/mcp.md``
→ "Install the Claude Code plugin"):

- **zip** (marketplace ``runspec``): the docs deploy zips a copy whose server
  command is ``uvx --from runspec-mcp runspec-mcp serve``, so the plugin works
  with no separate ``pip install`` (uv fetches the gateway from the package
  index). Built by ``scripts/build_claude_marketplace.py``.
- **pip** (marketplace ``runspec-pip``): a ``command`` source runs
  ``runspec-mcp claude-plugin``, which renders a copy into the gateway's app dir
  pointing at *this* install's absolute binary and Python, and prints its path —
  for machines that install from a private index and have no ``uv``.

Both keep the plugin name ``runspec-mcp``, so tool names
(``mcp__plugin_runspec-mcp_fleet__…``) and the autonomy hook's matcher are the
same whichever route installed it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from . import __version__

PLUGIN_NAME = "runspec-mcp"
SOURCE_DIR = Path(__file__).resolve().parent / "plugin"
UVX_COMMAND = ["uvx", "--from", "runspec-mcp", "runspec-mcp", "serve"]
HOOK_SCRIPT = "${CLAUDE_PLUGIN_ROOT}/hooks/autonomy_gate.py"


def render(dest: Path, *, server: list[str], hook_python: str = "python3", version: str = __version__) -> Path:
    """Copy the plugin source into ``dest`` (replacing it) with the MCP server
    started by ``server`` (command + args) and the autonomy hook run by
    ``hook_python``. Stamps ``version`` (the package's by default). Returns ``dest``."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SOURCE_DIR, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    manifest_file = dest / ".claude-plugin" / "plugin.json"
    manifest: dict[str, Any] = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["name"] = PLUGIN_NAME
    manifest["version"] = version
    fleet = manifest["mcpServers"]["fleet"]
    fleet["command"], fleet["args"] = server[0], list(server[1:])
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    hooks_file = dest / "hooks" / "hooks.json"
    hooks: dict[str, Any] = json.loads(hooks_file.read_text(encoding="utf-8"))
    for matcher in hooks["hooks"]["PreToolUse"]:
        for hook in matcher["hooks"]:
            hook["command"] = f'"{hook_python}" "{HOOK_SCRIPT}"'
    hooks_file.write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8")
    return dest


def marketplace(name: str, source: dict[str, Any], *, description: str) -> dict[str, Any]:
    """A one-plugin ``marketplace.json`` for the plugin at ``source``."""
    return {
        "name": name,
        "owner": {"name": "runspec", "url": "https://runspec.app"},
        "description": description,
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": source,
                "description": ("Local and remote runspec runnables as MCP tools in Claude Code, from one config file, gated by their declared autonomy"),
                "homepage": "https://runspec.app/mcp/",
            }
        ],
    }

"""
runspec-mcp — connect Claude Code to local and remote runspec runnables.

A single local MCP *gateway* server that reads one config file (hosts + credential
metadata), discovers the runnables installed in every configured venv (local and
over SSH), and presents them all to Claude Code as MCP tools — with each
runnable's declared ``autonomy`` carried into Claude Code's permission layer and
credentials injected as environment variables the way runspec-console does.

The public surface is the ``runspec-mcp`` CLI (see ``cli.py``); the MCP server
is ``runspec-mcp serve``.
"""

from __future__ import annotations

__version__ = "0.1.0"

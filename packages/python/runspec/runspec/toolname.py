"""MCP tool name length: the 128-character limit and a stable way to fit it.

The MCP specification allows tool names of 1–128 characters. ``runspec serve``
names a tool after its runnable, and a runnable with subcommands becomes one tool
per leaf (``runnable_sub_sub``), so a long runnable name or a deep command tree
can go over. Gateways that add a host or venv prefix (runspec-mcp, the console's
MCP server) make that more likely.

A name over the limit is never cut silently: cutting can make two tools share a
name. :func:`fit_tool_name` keeps the start of the name and ends it with a short
hash of the whole name, so the result is unique and the same on every run (which
keeps a client's saved per-tool approvals working). Callers warn when they use it.

This module is shared by ``runspec serve``, ``runspec test``, runspec-mcp and the
console, and mirrored byte-for-byte by the Node pack (``src/toolname.ts``).
"""

from __future__ import annotations

import hashlib
from typing import Any

MCP_TOOL_NAME_MAX = 128
"""Longest tool name the MCP specification allows."""

_HASH_CHARS = 6


def fit_tool_name(name: str, limit: int = MCP_TOOL_NAME_MAX) -> str:
    """Return ``name`` unchanged when it fits ``limit``, else a shortened name.

    The shortened form is the start of ``name`` (trailing ``_``/``-`` dropped),
    ``_``, then the first six hex characters of the SHA-1 of the whole name, e.g.
    ``very_long_runnable_name_sub_comm_3f9a1c``. It is at most ``limit``
    characters, the same for the same input, and differs for different inputs
    that share a long start.
    """
    if len(name) <= limit:
        return name
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:_HASH_CHARS]
    keep = max(limit - _HASH_CHARS - 1, 0)
    head = name[:keep].rstrip("_-")
    return f"{head}_{digest}" if head else digest[:limit]


def too_long_message(name: str, fitted: str, limit: int = MCP_TOOL_NAME_MAX) -> str:
    """One line explaining that ``name`` was shortened to ``fitted``."""
    return f"tool name '{name}' is {len(name)} characters, over the MCP limit of {limit}; serving it as '{fitted}'. Shorten the runnable or command names to keep the full name."


def leaf_tool_names(name: str, spec: dict[str, Any]) -> list[str]:
    """Every MCP tool name ``runspec serve`` makes for one runnable: the runnable
    name, or one ``runnable_sub_sub`` name per leaf command. Matches
    :func:`runspec.serve._expand_tools`."""
    commands = spec.get("commands") or {}
    if not commands:
        return [name]
    out: list[str] = []
    for sub_name, sub_spec in commands.items():
        out.extend(leaf_tool_names(f"{name}_{sub_name}", sub_spec or {}))
    return out

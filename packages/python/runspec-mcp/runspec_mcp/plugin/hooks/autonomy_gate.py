#!/usr/bin/env python3
"""
autonomy_gate.py — PreToolUse hook carrying runspec autonomy into Claude Code.

Claude Code invokes this for every tool call matching the plugin's MCP tools
(see hooks.json). It maps the runnable's declared runspec autonomy onto a
Claude Code permission decision:

    autonomous  -> allow   (no prompt)
    confirm     -> ask     (user approves the call)
    supervised  -> ask
    manual      -> deny    (agents may not run it at all)

The autonomy for each tool comes from autonomy.json in the plugin root — a
manifest baked from the remote venv's `runspec serve` tools/list (which emits
x-autonomy per tool). Regenerate it with scripts/generate_manifest.py whenever
the remote runnables change.

Per-arg escalation is honoured when the manifest carries per-arg autonomy
(`args: {name: level}`): the most restrictive level among the runnable and all
*provided* args wins — the same rule as runspec's `effective_autonomy()`.

Fail-closed: an unknown tool, a missing/broken manifest, or any unexpected
error resolves to "ask", never "allow". The remote-side denylist and
`autonomy = "manual"` enforcement remain authoritative regardless of this hook.

Input (stdin):  Claude Code hook JSON — {"tool_name": ..., "tool_input": {...}}
Output (stdout): {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                  "permissionDecision": ..., "permissionDecisionReason": ...}}
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Escalation order — must match runspec.inference.AUTONOMY_LEVELS.
AUTONOMY_LEVELS = ("autonomous", "confirm", "supervised", "manual")
AUTONOMY_RANK = {level: i for i, level in enumerate(AUTONOMY_LEVELS)}

DECISION = {
    "autonomous": "allow",
    "confirm": "ask",
    "supervised": "ask",
    "manual": "deny",
}


def manifest_path() -> Path:
    """autonomy.json in the plugin root: env override, CLAUDE_PLUGIN_ROOT, or
    relative to this script (hooks/ is one level below the root)."""
    override = os.environ.get("RUNSPEC_PLUGIN_MANIFEST")
    if override:
        return Path(override)
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        return Path(plugin_root) / "autonomy.json"
    return Path(__file__).resolve().parent.parent / "autonomy.json"


def bare_tool_name(full_name: str) -> str:
    """Strip the Claude Code plugin prefix: mcp__plugin_<plugin>_<server>__<tool>.

    Splitting on the double underscore is safe because runspec leaf tool names
    are single-underscore joined; rejoin defensively in case a tool name ever
    contains one.
    """
    if not full_name.startswith("mcp__"):
        return full_name
    parts = full_name.split("__")
    if len(parts) < 3:
        return full_name
    return "__".join(parts[2:])


def effective_autonomy(tool_entry: dict[str, Any], tool_input: dict[str, Any]) -> str:
    """Most restrictive of the runnable's autonomy and any provided arg's
    autonomy — mirrors runspec.inference.effective_autonomy()."""
    effective = tool_entry.get("autonomy", "confirm")
    if effective not in AUTONOMY_RANK:
        return "confirm"
    arg_levels: dict[str, Any] = tool_entry.get("args") or {}
    for arg_name, level in arg_levels.items():
        if level not in AUTONOMY_RANK:
            continue
        value = tool_input.get(arg_name)
        if value is None:
            value = tool_input.get(arg_name.replace("-", "_"))
        if value is None:
            continue
        if AUTONOMY_RANK[level] > AUTONOMY_RANK[effective]:
            effective = level
    return effective


def decide(payload: dict[str, Any]) -> tuple[str, str]:
    tool_name = bare_tool_name(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}

    path = manifest_path()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "ask", (
            f"runspec autonomy manifest not found at {path} — "
            "run scripts/generate_manifest.py to bake it; asking to be safe"
        )
    except (OSError, json.JSONDecodeError) as exc:
        return "ask", f"runspec autonomy manifest unreadable ({exc}); asking to be safe"

    tools = manifest.get("tools") or {}
    entry = tools.get(tool_name)
    if entry is None:
        return "ask", (
            f"'{tool_name}' is not in the autonomy manifest (added on the remote "
            "since it was generated?) — regenerate with scripts/generate_manifest.py"
        )

    level = effective_autonomy(entry, tool_input)
    decision = DECISION.get(level, "ask")
    reason = f"runspec autonomy = {level!r} for '{tool_name}'"
    extra = entry.get("reason")
    if extra:
        reason += f" ({extra})"
    if decision == "deny":
        reason += " — manual runnables are operator-only, run it yourself on the host"
    return decision, reason


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        decision, reason = decide(payload)
    except Exception as exc:  # fail closed, never crash the tool call pipeline
        decision, reason = "ask", f"autonomy gate error ({exc}); asking to be safe"

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()

"""
emitutil.py — Shared helpers for the `runspec emit` target generators.

Both `rundeckgen` (Rundeck jobs) and `ansiblegen` (Ansible modules) turn a
runnable's inferred spec into one artefact per **leaf command** and pass secrets
through the runspec env channel. Those two pieces are platform-agnostic, so they
live here and are re-exported by each generator for back-compat.
"""

from __future__ import annotations

from typing import Any

from runspec.inference import infer_arg


def secret_env_var(runnable: str, name: str) -> str:
    """The runspec secret channel var for an arg — `RUNSPEC_<RUNNABLE>_ARG_<NAME>`.

    Mirrors `parser._apply_env` / `serve._env_key`: keyed on the top-level
    runnable name (the entry point), not the leaf command, with hyphens →
    underscores and uppercased."""
    r = runnable.upper().replace("-", "_")
    n = name.upper().replace("-", "_")
    return f"RUNSPEC_{r}_ARG_{n}"


def expand_leaves(runnable: str, script: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a runnable's subcommand tree into one entry per leaf command.

    A runnable with no `commands` yields a single leaf (itself). Otherwise one
    leaf per fully-pathed command, with args = the root (global) args merged
    with each command's own args along the path, child overriding parent on a
    name clash — the same inheritance `parser._resolve_subcommand` applies at
    runtime. Args are run through `infer_arg` so inferred types are reflected.

    Each entry: `{name, description, command_path, args}` where `name` is the
    serve-style leaf id (`cron_add`) and `command_path` the tokens to append
    after the runnable binary (`["add"]`).
    """
    root_desc = script.get("description") or ""
    leaves: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], path: list[str], merged: dict[str, Any]) -> None:
        args = dict(merged)
        args.update(node.get("args", {}))
        commands = node.get("commands") or {}
        if not commands:
            name = "_".join([runnable, *path]) if path else runnable
            leaves.append(
                {
                    "name": name,
                    "description": node.get("description") or root_desc,
                    "command_path": list(path),
                    "args": {k: infer_arg(v) for k, v in args.items()},
                }
            )
            return
        for cmd_name, cmd_node in commands.items():
            walk(cmd_node, [*path, cmd_name], args)

    walk(script, [], {})
    return leaves

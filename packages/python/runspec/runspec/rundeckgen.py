"""
rundeckgen.py — Render Rundeck job definitions (JSON) from runspec runnables.

Pure module (stdlib only, no filesystem side effects) so its output is
deterministic — identical input yields byte-identical JSON, which keeps the
`runspec emit --rundeck --check` drift diff stable. The CLI (`cli.cmd_emit`)
owns discovery and disk I/O; this module owns the render, mirroring the
`stubgen` / `cmd_stubs` split.

What it emits
-------------
One Rundeck job per **leaf command** (a runnable with subcommands flattens to
one job per fully-pathed leaf, like `runspec serve`). Every job runs a single
Bash **script step** — never an `exec`/command step, because a static command
string can't conditionally include a `--flag` from a boolean option. The script
reads each option from its `RD_OPTION_<NAME>` environment variable (not inline
`${option.x}` substitution — that avoids script injection and is what lets a
secret ride the env channel without ever landing on the command line).

Targets Rundeck 4.17.6. The JSON job-definition format mirrors the YAML job
structure and imports via the API (`Content-Type: application/json`) or
`rd jobs load -F json`.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from runspec.inference import infer_arg

# Fixed namespace so a job's UUID is a stable function of (project, group, job
# name) — re-emitting and re-importing then *updates* the same Rundeck job
# instead of creating a duplicate.
_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://runspec.app/rundeck")

# runspec type → does it need special option handling? Everything renders as a
# plain Rundeck text option (Rundeck has no native int/float/bool/path option
# type; `choice`/`flag` use `values` + `enforced`, numbers use a `regex`).
_NUMERIC_REGEX = {
    "int": r"^-?\d+$",
    "float": r"^-?\d+(\.\d+)?$",
}


# ── name helpers ──────────────────────────────────────────────────────────────


def rd_env_var(name: str) -> str:
    """Option name → the `RD_OPTION_<NAME>` env var Rundeck exposes it as.

    Rundeck uppercases the option name and replaces every non-alphanumeric
    character with `_` (so `node-filter` → `RD_OPTION_NODE_FILTER`)."""
    return "RD_OPTION_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def secret_env_var(runnable: str, name: str) -> str:
    """The runspec secret channel var for an arg — `RUNSPEC_<RUNNABLE>_ARG_<NAME>`.

    Mirrors `parser._apply_env` / `serve._env_key`: keyed on the top-level
    runnable name (the entry point), not the leaf command, with hyphens →
    underscores and uppercased."""
    r = runnable.upper().replace("-", "_")
    n = name.upper().replace("-", "_")
    return f"RUNSPEC_{r}_ARG_{n}"


def job_filename(job_name: str) -> str:
    """Per-job output file name."""
    return f"{job_name}.json"


# ── leaf expansion ────────────────────────────────────────────────────────────


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


# ── option rendering ──────────────────────────────────────────────────────────


def _option_description(arg: dict[str, Any]) -> str:
    """Base description plus a compact note for constraints Rundeck can't model
    natively (range, pattern, positional order, rest passthrough), so the info
    is at least visible on the run form."""
    parts: list[str] = []
    if arg.get("description"):
        parts.append(str(arg["description"]))
    notes: list[str] = []
    if arg.get("type") == "rest":
        notes.append("space-separated; passed to the runnable after --")
    if arg.get("position") is not None:
        notes.append(f"positional #{arg['position']}")
    rng = arg.get("range")
    if rng:
        notes.append(f"range {rng[0]}–{rng[1]}")
    if arg.get("pattern"):
        notes.append(f"pattern {arg['pattern']}")
    if notes:
        parts.append("(" + "; ".join(notes) + ")")
    return " ".join(parts)


def render_option(name: str, arg: dict[str, Any]) -> dict[str, Any]:
    """Render one runspec arg as a Rundeck job option.

    - `password` → a **secure** option (`secure: true, valueExposed: true`): the
      operator enters it at run time, it is encrypted, kept off the command
      line, and exposed to the script only as `RD_OPTION_<NAME>`. Never carries
      a default `value`.
    - `flag`     → text option constrained to `["true", "false"]` (`enforced`),
      defaulting to the flag's boolean default.
    - `choice`/`options` → text option constrained to the choices (`enforced`).
    - `int`/`float` → text option with a validating `regex`.
    - everything else → plain text option.
    """
    opt: dict[str, Any] = {"name": name}
    desc = _option_description(arg)
    if desc:
        opt["description"] = desc

    arg_type = arg.get("type", "str")

    if arg_type == "password":
        opt["secure"] = True
        opt["valueExposed"] = True
        if arg.get("required"):
            opt["required"] = True
        return opt

    if arg.get("required"):
        opt["required"] = True

    default = arg.get("default")

    if arg_type == "flag":
        opt["values"] = ["true", "false"]
        opt["enforced"] = True
        opt["value"] = "true" if default is True else "false"
    elif arg.get("options"):
        opt["values"] = [str(o) for o in arg["options"]]
        opt["enforced"] = True
        if default is not None:
            opt["value"] = str(default)
    else:
        if default is not None and arg_type != "rest":
            opt["value"] = str(default)
        regex = _NUMERIC_REGEX.get(arg_type)
        if regex:
            opt["regex"] = regex

    return opt


# ── script step ───────────────────────────────────────────────────────────────


def _arg_category(arg: dict[str, Any]) -> str:
    """One of: secret, rest, flag, positional, named."""
    t = arg.get("type", "str")
    if t == "password":
        return "secret"
    if t == "rest":
        return "rest"
    if t == "flag":
        return "flag"
    if arg.get("position") is not None:
        return "positional"
    return "named"


def render_script(runnable: str, command_path: list[str], args: dict[str, Any], invocation: str) -> str:
    """Build the Bash script that reconstructs the CLI invocation from options.

    Values are read from `RD_OPTION_*` env vars (defaulted to empty so an unset
    optional arg simply contributes nothing). Named args are emitted only when
    non-empty; flags only when `"true"`; positionals in `position` order; a
    `rest` arg is word-split after a literal `--`. `password` args never enter
    the command line — they are exported on the runspec secret channel
    (`RUNSPEC_<RUNNABLE>_ARG_<NAME>`) instead.
    """
    named: list[tuple[str, dict[str, Any]]] = []
    flags: list[tuple[str, dict[str, Any]]] = []
    positionals: list[tuple[str, dict[str, Any]]] = []
    rests: list[tuple[str, dict[str, Any]]] = []
    secrets: list[tuple[str, dict[str, Any]]] = []
    for name, arg in args.items():
        {"named": named, "flag": flags, "positional": positionals, "rest": rests, "secret": secrets}[_arg_category(arg)].append((name, arg))
    positionals.sort(key=lambda pair: pair[1].get("position") or 0)

    lines = ["#!/bin/bash", "set -e", "args=()"]

    for name, _arg in named:
        env = rd_env_var(name)
        lines.append(f'if [ -n "${{{env}:-}}" ]; then args+=(--{name} "${{{env}:-}}"); fi')

    for name, _arg in flags:
        env = rd_env_var(name)
        lines.append(f'if [ "${{{env}:-}}" = "true" ]; then args+=(--{name}); fi')

    for name, _arg in positionals:
        env = rd_env_var(name)
        lines.append(f'if [ -n "${{{env}:-}}" ]; then args+=("${{{env}:-}}"); fi')

    for name, _arg in rests:
        env = rd_env_var(name)
        # Intentionally unquoted so the value word-splits into separate args.
        lines.append(f'if [ -n "${{{env}:-}}" ]; then args+=(-- ${{{env}:-}}); fi')

    for name, _arg in secrets:
        env = rd_env_var(name)
        lines.append(f'export {secret_env_var(runnable, name)}="${{{env}:-}}"')

    invoke = " ".join([invocation, *command_path, '"${args[@]}"'])
    lines.append(f"exec {invoke}")
    return "\n".join(lines)


# ── notifications ─────────────────────────────────────────────────────────────

_NOTIFY_TRIGGERS = ("onsuccess", "onfailure", "onstart")


def render_notifications(spec: dict[str, Any]) -> dict[str, Any]:
    """Render a `meta.rundeck.notifications` table into a Rundeck `notification`
    block. Each trigger (`onsuccess`/`onfailure`/`onstart`) may carry `email`
    (`{recipients, subject?, attachLog?}`) and/or `webhook`/`url` (`{urls}`)."""
    out: dict[str, Any] = {}
    for trigger in _NOTIFY_TRIGGERS:
        cfg = spec.get(trigger)
        if not isinstance(cfg, dict):
            continue
        block: dict[str, Any] = {}
        email = cfg.get("email")
        if isinstance(email, str):
            block["email"] = {"recipients": email}
        elif isinstance(email, dict) and email.get("recipients"):
            e: dict[str, Any] = {"recipients": email["recipients"]}
            if email.get("subject"):
                e["subject"] = email["subject"]
            if email.get("attachLog"):
                e["attachLog"] = True
            block["email"] = e
        urls = cfg.get("webhook") or cfg.get("url") or cfg.get("urls")
        if isinstance(urls, str):
            block["webhook"] = {"urls": urls}
        elif isinstance(urls, dict) and urls.get("urls"):
            block["webhook"] = {"urls": urls["urls"]}
        if block:
            out[trigger] = block
    return out


# ── settings resolution ───────────────────────────────────────────────────────


def _first(*values: Any) -> Any:
    """First non-None value."""
    for v in values:
        if v is not None:
            return v
    return None


def merge_settings(
    runnable: str,
    cli_opts: dict[str, Any],
    config_meta: dict[str, Any],
    runnable_meta: dict[str, Any],
) -> dict[str, Any]:
    """Resolve per-runnable Rundeck settings.

    Precedence: CLI flag > `[<runnable>.meta.rundeck]` > `[config.meta.rundeck]`
    > built-in default. `cli_opts` carries the emit flags (`venv`, `group`,
    `node_filter`, `project`); the two meta dicts are read from the raw TOML.
    """
    venv = _first(cli_opts.get("venv"), runnable_meta.get("venv"), config_meta.get("venv"))
    invocation = f"{str(venv).rstrip('/')}/bin/{runnable}" if venv else runnable

    node_filter = _first(cli_opts.get("node_filter"), runnable_meta.get("node_filter"), config_meta.get("node_filter"))

    # Random-subset load-balancing defaults ON when a node filter is present, so
    # each run lands on one random node of the set; OFF (server-local) otherwise.
    orchestrator = _first(runnable_meta.get("orchestrator"), config_meta.get("orchestrator"))
    if orchestrator is None:
        orchestrator = "subset" if node_filter else "none"

    notifications = _first(runnable_meta.get("notifications"), config_meta.get("notifications"))

    return {
        "invocation": invocation,
        "venv": venv,
        "group": _first(cli_opts.get("group"), runnable_meta.get("group"), config_meta.get("group"), "runspec"),
        "project": _first(cli_opts.get("project"), config_meta.get("project"), "runspec"),
        "node_filter": node_filter,
        "orchestrator": orchestrator,
        "threadcount": _first(runnable_meta.get("threadcount"), config_meta.get("threadcount"), 1),
        "schedule": _first(runnable_meta.get("schedule"), config_meta.get("schedule")),
        "notifications": notifications,
        "loglevel": _first(runnable_meta.get("loglevel"), config_meta.get("loglevel"), "INFO"),
        "strategy": _first(runnable_meta.get("strategy"), config_meta.get("strategy"), "node-first"),
        "keepgoing": bool(_first(runnable_meta.get("keepgoing"), config_meta.get("keepgoing"), False)),
        "timeout": _first(runnable_meta.get("timeout"), config_meta.get("timeout")),
        "retry": _first(runnable_meta.get("retry"), config_meta.get("retry")),
    }


# ── job + file assembly ───────────────────────────────────────────────────────


def render_job(runnable: str, leaf: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Assemble one Rundeck job object from a leaf command and resolved settings."""
    job_name = leaf["name"]
    args = leaf["args"]
    group = settings["group"]

    script = render_script(runnable, leaf["command_path"], args, settings["invocation"])

    job: dict[str, Any] = {
        "name": job_name,
        "description": leaf.get("description") or "",
        "group": group,
        "loglevel": settings["loglevel"],
        "uuid": str(uuid.uuid5(_UUID_NAMESPACE, f"{settings['project']}/{group}/{job_name}")),
        "executionEnabled": True,
        "sequence": {
            "keepgoing": settings["keepgoing"],
            "strategy": settings["strategy"],
            "commands": [{"script": script, "scriptInterpreter": "/bin/bash"}],
        },
    }

    options = [render_option(name, arg) for name, arg in args.items()]
    if options:
        job["options"] = options

    if settings["node_filter"]:
        job["nodefilters"] = {
            "filter": settings["node_filter"],
            "dispatch": {"threadcount": int(settings["threadcount"]), "keepgoing": settings["keepgoing"]},
        }
        if settings["orchestrator"] == "subset":
            job["orchestrator"] = {"type": "subset", "configuration": {"count": "1"}}

    if settings["schedule"]:
        job["schedule"] = {"crontab": str(settings["schedule"])}
        job["scheduleEnabled"] = True

    if settings["notifications"]:
        notification = render_notifications(settings["notifications"])
        if notification:
            job["notification"] = notification

    if settings["timeout"] is not None:
        job["timeout"] = str(settings["timeout"])
    if settings["retry"] is not None:
        job["retry"] = str(settings["retry"])

    return job


def render_runnable(runnable: str, script: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """All Rundeck job objects for one runnable (one per leaf command)."""
    return [render_job(runnable, leaf, settings) for leaf in expand_leaves(runnable, script)]


def dumps(jobs: list[dict[str, Any]]) -> str:
    """Serialize a list of job objects as a Rundeck job-definition JSON array.

    `sort_keys=True` keeps output byte-stable for `--check` (list order — e.g.
    the `options` array — is preserved; only object keys are sorted)."""
    return json.dumps(jobs, indent=2, sort_keys=True) + "\n"

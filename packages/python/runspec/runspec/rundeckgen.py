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
Bash **script step** that assigns each option to its runspec env var —
`RUNSPEC_<RUNNABLE>_ARG_<NAME>=@option.<name>@` — and runs the binary with no
arg values on its command line (they would show in `ps`). It doesn't read
`RD_OPTION_*` variables (those need `AcceptEnv RD_*` on every node's sshd).

Requires Rundeck 5.20+, which shell-escapes option values substituted into a
job. Accepted tradeoff: Rundeck writes the substituted script, secrets included,
to a temp file on the node, readable by the Rundeck SSH user until the step
ends.

The JSON job-definition format mirrors the YAML job structure and imports via
the API (`Content-Type: application/json`) or `rd jobs load -F json`.
"""

from __future__ import annotations

import json
import re
import shlex
import uuid
from typing import Any

from runspec.become import ENV_PRELUDE, PY_ENV_PRELUDE

# Re-exported so `rundeckgen.expand_leaves` / `rundeckgen.secret_env_var` keep
# working; the generic implementations live in `emitutil` (shared with ansiblegen).
from runspec.emitutil import expand_leaves, secret_env_var

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


def job_filename(job_name: str) -> str:
    """Per-job output file name."""
    return f"{job_name}.json"


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
      operator enters it at run time, it is encrypted, kept out of the job log,
      and reaches the script only through its `@option.<name>@` token. Never
      carries a default `value`.
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

# Bash helper that writes the SPEC "env frame" for the named shell variables to
# stdout: one `NAME=\0ooo…` line per non-empty variable (every byte of the value
# as an octal escape), then a `.` line. Only shell builtins see a value
# (`printf`), and `od`/`tr`/`sed` read it from a pipe, so no value reaches any
# process's argv. The SPEC env prelude on the far side of `sudo` reads it back.
_FRAME_FUNC = (
    "rs_frame() {\n"
    "  local rs_var rs_val\n"
    '  for rs_var in "$@"; do\n'
    "    rs_val=${!rs_var}\n"
    '    [ -n "$rs_val" ] || continue\n'
    "    printf '%s=%s\\n' \"$rs_var\" \"$(printf '%s' \"$rs_val\" | od -An -v -to1 | tr -d ' \\n' | sed 's/.../\\\\0&/g')\"\n"
    "  done\n"
    "  printf '.\\n'\n"
    "}"
)


# Delimiter of the per-value heredocs. Each body line starts with `x`, so no
# substituted value can turn its line into this delimiter.
_VALUE_EOF = "RUNSPEC_VALUE"

# The script's second line: count the script file's own non-blank lines and
# refuse to run unless the count is the one the emitter wrote. A Rundeck option
# value is substituted into the script text before it reaches the node, and the
# only way one can escape its heredoc is a line break, which adds a line. The
# guard runs before Bash reads any value line. Blank lines are not counted, so
# a trailing newline Rundeck adds (or an empty line in a value) changes nothing;
# an unreadable script file fails closed.
_GUARD_COUNT = "@RS_LINES@"
_GUARD = (
    f'[ "$(awk \'NF {{ n++ }} END {{ print n + 0 }}\' "${{BASH_SOURCE[0]:-$0}}")" = {_GUARD_COUNT} ] || '
    "{ echo 'runspec: a Rundeck option value contains a line break; refusing to run' >&2; exit 64; }"
)


_RUN_AS = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*")


def rd_option_token(name: str) -> str:
    """Option name → the `@option.<name>@` token Rundeck substitutes into an
    inline script before copying it to the node."""
    return f"@option.{name}@"


def _env_prelude_argv(invocation: str, venv: str | None) -> list[str]:
    """The env-prelude wrapper for a run behind `sudo` (SPEC "Command
    building"): the venv's own `bin/python` when the venv is known, so a sudoers
    rule allowing only the venv's `bin/` still covers it; else POSIX `sh`."""
    if venv:
        return [shlex.quote(f"{str(venv).rstrip('/')}/bin/python"), "-c", '"$RS_PRELUDE"']
    return ["sh", "-c", '"$RS_PRELUDE"', "sh"]


def render_script(
    runnable: str,
    command_path: list[str],
    args: dict[str, Any],
    invocation: str,
    run_as: str | None = None,
    venv: str | None = None,
) -> str:
    """Build the Bash script that runs the runnable with the job's option values.

    Every value — secret or not — reaches the runnable on runspec's env channel
    (`RUNSPEC_<RUNNABLE>_ARG_<NAME>`), never on a command line, and nothing
    relies on `RD_OPTION_*` variables (so the node's sshd needs no
    `AcceptEnv RD_*`).

    Rundeck substitutes each `@option.<name>@` token into the script text before
    copying it to the node, and before 5.20 it does not escape the value. So the
    script never lets the shell parse a value: each token sits alone on its own
    line inside a quoted heredoc (`<<'RUNSPEC_VALUE'`), which Bash treats as
    data, and `read` takes that one line into the variable. A heredoc body ends
    only at a line that is exactly the delimiter, which a value can only reach by
    containing a line break — so the script's second line counts its own
    non-blank lines and refuses to run if a value added any. The `x` in front of
    each token keeps an empty or delimiter-shaped value from ending the heredoc.

    Without `run_as` the non-empty values are exported (an empty option stays
    unset so the runnable's own default applies) and the binary is exec'd. With
    `run_as` the script runs `sudo -n -u <run_as>` and, because sudo resets the
    environment, writes the values as the SPEC env frame on the child's stdin,
    read by the fixed env prelude — values never appear in argv or the sudo log.
    The command line carries only the binary and the subcommand path.
    """
    env_vars = [(secret_env_var(runnable, name), name) for name in args]
    names = " ".join(var for var, _ in env_vars)

    lines = ["#!/bin/bash", _GUARD, "set -e"]
    for var, name in env_vars:
        lines.append(f"IFS= read -r {var} <<'{_VALUE_EOF}'")
        lines.append(f"x{rd_option_token(name)}")
        lines.append(_VALUE_EOF)
        lines.append(f"{var}=${{{var}#x}}")

    cmd = " ".join(shlex.quote(part) for part in [invocation, *command_path])
    if not run_as:
        if env_vars:
            lines.append(f"for rs_var in {names}; do")
            lines.append('  if [ -n "${!rs_var}" ]; then export "$rs_var"; else unset "$rs_var"; fi')
            lines.append("done")
        lines.append(f"exec {cmd}")
        return _finish(lines)

    if not _RUN_AS.fullmatch(run_as):
        raise ValueError(f"run_as {run_as!r} is not a valid account name")
    become = f"sudo -n -u {run_as}"
    if not env_vars:
        lines.append(f"exec {become} {cmd}")
        return _finish(lines)

    prelude = PY_ENV_PRELUDE if venv else ENV_PRELUDE
    lines.append(f"RS_PRELUDE='{prelude}'")
    lines.append(_FRAME_FUNC)
    wrapped = " ".join([*_env_prelude_argv(invocation, venv), cmd])
    lines.append(f"rs_frame {names} | {become} {wrapped}")
    return _finish(lines)


def _finish(lines: list[str]) -> str:
    """Join the script and fill in the guard's expected non-blank line count."""
    script = "\n".join(lines)
    count = sum(1 for line in script.split("\n") if line.strip())
    return script.replace(_GUARD_COUNT, str(count), 1)


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
    default_run_as: Any = None,
) -> dict[str, Any]:
    """Resolve per-runnable Rundeck settings.

    Precedence: CLI flag > `[<runnable>.meta.rundeck]` > `[config.meta.rundeck]`
    > built-in default. `cli_opts` carries the emit flags (`venv`, `group`,
    `node_filter`, `project`, `run_as`); the two meta dicts are read from the
    raw TOML. `run_as` falls back to `default_run_as` — the runnable's own
    literal `run_as` account, when it names one.
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

    run_as = _first(cli_opts.get("run_as"), runnable_meta.get("run_as"), config_meta.get("run_as"), literal_run_as(default_run_as))

    return {
        "invocation": invocation,
        "venv": venv,
        "run_as": run_as or None,
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


def literal_run_as(value: Any) -> str | None:
    """A runnable's `run_as` when it names a fixed account; `None` for the
    forms only the node can resolve (`@owner`, `$ENV`, a per-host table) and for
    the `""` opt-out."""
    if isinstance(value, str) and value and value[0] not in "@$":
        return value
    return None


# ── job + file assembly ───────────────────────────────────────────────────────


def render_job(runnable: str, leaf: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Assemble one Rundeck job object from a leaf command and resolved settings."""
    job_name = leaf["name"]
    args = leaf["args"]
    group = settings["group"]

    script = render_script(
        runnable,
        leaf["command_path"],
        args,
        settings["invocation"],
        run_as=settings.get("run_as"),
        venv=settings.get("venv"),
    )

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

"""
ansiblegen.py — Render Ansible modules (Python) from runspec runnables.

Pure module (stdlib only, no filesystem side effects) so its output is
deterministic — identical input yields byte-identical source, which keeps the
`runspec emit --ansible --check` drift diff stable. The CLI (`cli.cmd_emit`)
owns discovery and disk I/O; this module owns the render, mirroring the
`stubgen` / `cmd_stubs` and `rundeckgen` split.

What it emits
-------------
One Ansible module `.py` per **leaf command** (a runnable with subcommands
flattens to one module per fully-pathed leaf, like `runspec serve`). Each module
carries a `DOCUMENTATION` / `EXAMPLES` / `RETURN` block (which drives the AAP
survey form, ansible-lint, and IDE support), an `argument_spec` built from the
runnable's args, and a small constant body that rebuilds the CLI invocation from
`module.params` and shells out to the runnable.

Design points
- **`password` args** become `no_log` params passed through the runspec secret
  channel env var (`RUNSPEC_<RUNNABLE>_ARG_<NAME>`), never on the command line.
- **Idempotency:** a runnable whose JSON stdout carries a `changed` key drives
  Ansible's `changed`; otherwise a zero exit is reported as `changed=true`.
  `check_mode` short-circuits to `changed=false` (conservative).
- **Non-interactive:** the module injects `RUNSPEC_AUTONOMY=autonomous` so a
  `confirm` runnable runs in a playbook without prompting.
"""

from __future__ import annotations

from typing import Any

from runspec.emitutil import expand_leaves, secret_env_var

# runspec type → Ansible argument_spec `type`. Rundeck had no native types;
# Ansible does, so this is a direct map (choice adds `choices`, rest is a list).
_SPEC_TYPE = {
    "str": "str",
    "password": "str",
    "int": "int",
    "float": "float",
    "flag": "bool",
    "path": "path",
    "choice": "str",
    "rest": "list",
}


# ── name helpers ──────────────────────────────────────────────────────────────


def module_name(leaf_name: str) -> str:
    """Leaf id → a valid Ansible module name (a Python identifier).

    Ansible module files are imported, and its parameter/variable names must be
    valid identifiers, so hyphens become underscores (`cert-check` →
    `cert_check`, `filesystem_read-text-file` → `filesystem_read_text_file`)."""
    return leaf_name.replace("-", "_")


def param_name(arg_name: str) -> str:
    """Arg name → Ansible parameter name (a valid variable: hyphens → underscores)."""
    return arg_name.replace("-", "_")


def module_filename(leaf_name: str) -> str:
    """Per-module output file name."""
    return f"{module_name(leaf_name)}.py"


# ── Python-literal rendering (for the generated argument_spec / ARGS) ──────────


def _py(value: Any, indent: int = 0) -> str:
    """Render a JSON-ish value as a Python literal (True/False/None, not the JSON
    spellings), pretty-printed for dicts/lists so the generated module reads
    naturally and diffs stably."""
    pad = "    " * indent
    pad1 = "    " * (indent + 1)
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        items = ",\n".join(pad1 + _py(v, indent + 1) for v in value)
        return "[\n" + items + ",\n" + pad + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = ",\n".join(f"{pad1}{_py(k)}: {_py(v, indent + 1)}" for k, v in value.items())
        return "{\n" + items + ",\n" + pad + "}"
    return _py(str(value))


# ── YAML-scalar rendering (for the DOCUMENTATION / EXAMPLES / RETURN blocks) ───


def _yaml_scalar(value: Any) -> str:
    """A YAML scalar. Strings are double-quoted+escaped so a description
    containing a colon or `#` can't break the doc block; bools/numbers render
    bare."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


# ── argument_spec + argv metadata ─────────────────────────────────────────────


def arg_spec_entry(arg: dict[str, Any]) -> dict[str, Any]:
    """One arg → its Ansible `argument_spec` entry."""
    arg_type = arg.get("type", "str")
    entry: dict[str, Any] = {"type": _SPEC_TYPE.get(arg_type, "str")}
    if arg_type == "rest":
        entry["elements"] = "str"
    if arg_type == "password":
        entry["no_log"] = True
    if arg.get("options"):
        entry["choices"] = [str(o) for o in arg["options"]]
    if arg.get("required"):
        entry["required"] = True
    default = arg.get("default")
    if default is not None and arg_type != "rest":
        entry["default"] = default
    return entry


def _arg_kind(arg: dict[str, Any]) -> str:
    """named / flag / positional / rest / secret — how the arg maps onto argv."""
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


def _args_metadata(runnable: str, args: dict[str, Any]) -> list[list[Any]]:
    """The `ARGS` table the generated module loops over to rebuild argv.

    Each row: [param, kind, cli_flag, position, secret_env]. `param` is the
    Ansible param name; `cli_flag` the runnable's declared flag; `secret_env` the
    runspec channel var (secrets only)."""
    rows: list[list[Any]] = []
    for name, arg in args.items():
        kind = _arg_kind(arg)
        rows.append(
            [
                param_name(name),
                kind,
                f"--{name}",
                arg.get("position"),
                secret_env_var(runnable, name) if kind == "secret" else None,
            ]
        )
    return rows


# ── documentation blocks ──────────────────────────────────────────────────────


def _short_description(description: str) -> str:
    """Ansible `short_description`: a one-line summary, no trailing period."""
    first = (description or "").strip().splitlines()[0] if description else ""
    return first.rstrip(".") or "Run the runspec runnable"


def render_documentation(mod: str, description: str, args: dict[str, Any]) -> str:
    """The `DOCUMENTATION` YAML body (drives the AAP survey form + ansible-doc)."""
    lines = [
        f"module: {mod}",
        f"short_description: {_yaml_scalar(_short_description(description))}",
        "description:",
        f"  - {_yaml_scalar(description or _short_description(description))}",
    ]
    if args:
        lines.append("options:")
        for name, arg in args.items():
            entry = arg_spec_entry(arg)
            lines.append(f"  {param_name(name)}:")
            lines.append(f"    description: {_yaml_scalar(arg.get('description') or name)}")
            lines.append(f"    required: {'true' if entry.get('required') else 'false'}")
            lines.append(f"    type: {entry['type']}")
            if entry.get("type") == "list":
                lines.append("    elements: str")
            if entry.get("choices"):
                lines.append("    choices: [" + ", ".join(_yaml_scalar(c) for c in entry["choices"]) + "]")
            if "default" in entry:
                lines.append(f"    default: {_yaml_scalar(entry['default'])}")
    return "\n".join(lines)


def render_examples(mod: str, args: dict[str, Any]) -> str:
    """A minimal `EXAMPLES` YAML body — the module with its required args.

    `password` args are omitted: a secret belongs in Vault / the environment, not
    inlined in an example playbook."""
    shown = {name: arg for name, arg in args.items() if arg.get("type") != "password"}
    required = {name: arg for name, arg in shown.items() if arg_spec_entry(arg).get("required")}
    sample = required or shown
    lines = [f"- name: Run {mod}", f"  {mod}:"]
    for name, arg in list(sample.items())[:4]:
        lines.append(f"    {param_name(name)}: {_yaml_scalar(arg.get('default') if arg.get('default') is not None else 'value')}")
    if not sample:
        lines[-1] = f"  {mod}: {{}}"
    return "\n".join(lines)


_RETURN_BLOCK = """\
changed:
  description: Whether the runnable reported a change.
  type: bool
  returned: always
rc:
  description: Return code of the runnable process.
  type: int
  returned: always
stdout:
  description: Captured standard output from the runnable.
  type: str
  returned: always
stderr:
  description: Captured standard error from the runnable.
  type: str
  returned: always
result:
  description: Parsed JSON output when the runnable emits an object.
  type: dict
  returned: when stdout is a JSON object"""


# ── module body (constant boilerplate) ────────────────────────────────────────

_BODY = """\
import json
import os
import subprocess

from ansible.module_utils.basic import AnsibleModule


def build_command(params):
    argv = [RUNNABLE, *COMMAND_PATH]
    positionals = []
    rest = []
    for param, kind, cli, position, _secret in ARGS:
        value = params.get(param)
        if kind == "secret" or value is None:
            continue
        if kind == "flag":
            if value:
                argv.append(cli)
        elif kind == "positional":
            positionals.append((position or 0, str(value)))
        elif kind == "rest":
            rest.extend(str(v) for v in value)
        else:
            argv.extend([cli, str(value)])
    for _, token in sorted(positionals):
        argv.append(token)
    if rest:
        argv.append("--")
        argv.extend(rest)
    return argv


def build_env(params):
    env = dict(os.environ)
    env["RUNSPEC_AUTONOMY"] = "autonomous"
    for param, kind, _cli, _position, secret_env in ARGS:
        if kind == "secret" and params.get(param) is not None:
            env[secret_env] = str(params[param])
    return env


def main():
    module = AnsibleModule(argument_spec=ARGUMENT_SPEC, supports_check_mode=True)
    if module.check_mode:
        module.exit_json(changed=False, skipped=True)
    argv = build_command(module.params)
    proc = subprocess.run(argv, capture_output=True, text=True, env=build_env(module.params))
    result = None
    changed = proc.returncode == 0
    try:
        parsed = json.loads(proc.stdout)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        result = parsed
        if "changed" in parsed:
            changed = bool(parsed["changed"])
    if proc.returncode != 0:
        module.fail_json(
            msg="%s exited with rc=%d" % (RUNNABLE, proc.returncode),
            rc=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    module.exit_json(
        changed=changed,
        rc=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        result=result,
    )


if __name__ == "__main__":
    main()
"""


# ── module assembly ───────────────────────────────────────────────────────────


def render_module(runnable: str, leaf: dict[str, Any], invocation: str) -> str:
    """The full Ansible module `.py` source for one leaf command."""
    mod = module_name(leaf["name"])
    args = leaf["args"]
    description = leaf.get("description") or ""

    argument_spec = {param_name(name): arg_spec_entry(arg) for name, arg in args.items()}
    args_meta = _args_metadata(runnable, args)

    return "".join(
        [
            "#!/usr/bin/python\n",
            "# -*- coding: utf-8 -*-\n",
            "# Generated by `runspec emit --ansible` from runspec.toml. Do not edit by hand.\n",
            "from __future__ import annotations\n\n",
            "DOCUMENTATION = r'''\n" + render_documentation(mod, description, args) + "\n'''\n\n",
            "EXAMPLES = r'''\n" + render_examples(mod, args) + "\n'''\n\n",
            "RETURN = r'''\n" + _RETURN_BLOCK + "\n'''\n\n",
            f"RUNNABLE = {_py(invocation)}\n",
            f"COMMAND_PATH = {_py(leaf['command_path'])}\n",
            "# Each row: [param, kind, cli_flag, position, secret_env]\n",
            f"ARGS = {_py(args_meta)}\n",
            f"ARGUMENT_SPEC = {_py(argument_spec)}\n\n\n",
            _BODY,
        ]
    )


def render_runnable(runnable: str, script: dict[str, Any], invocation: str) -> dict[str, str]:
    """All Ansible modules for one runnable (one per leaf), keyed by file name."""
    out: dict[str, str] = {}
    for leaf in expand_leaves(runnable, script):
        out[module_filename(leaf["name"])] = render_module(runnable, leaf, invocation)
    return out

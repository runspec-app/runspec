/**
 * ansiblegen.ts — Render Ansible modules (Python) from runspec runnables.
 *
 * Pure module (no filesystem side effects) so its output is deterministic —
 * identical input yields byte-identical source, which keeps the
 * `runspec emit --ansible --check` drift diff stable. The CLI (`cli.cmdEmit`)
 * owns discovery and disk I/O; this module owns the render.
 *
 * Ports `runspec/ansiblegen.py`. One Ansible module `.py` per **leaf command**
 * (subcommands flatten like `runspec serve`). Each module carries a
 * `DOCUMENTATION`/`EXAMPLES`/`RETURN` block (drives the AAP survey form,
 * ansible-lint, IDE support), an `argument_spec` from the args, and a small
 * constant body that rebuilds the CLI invocation from `module.params` and shells
 * out. `password` args become `no_log` params passed through the runspec secret
 * channel (`RUNSPEC_<RUNNABLE>_ARG_<NAME>`), never on the command line.
 */

import { ArgSpec, ScriptSpec } from './models';
import { Leaf, expandLeaves, secretEnvVar } from './emitutil';

// runspec type → Ansible argument_spec `type` (choice adds `choices`, rest is a list).
const SPEC_TYPE: Record<string, string> = {
  str: 'str',
  password: 'str',
  int: 'int',
  float: 'float',
  flag: 'bool',
  path: 'path',
  choice: 'str',
  rest: 'list',
};

// ── name helpers ──────────────────────────────────────────────────────────────

/** Leaf id → a valid Ansible module name (a Python identifier: hyphens → underscores). */
export function moduleName(leafName: string): string {
  return leafName.replace(/-/g, '_');
}

/** Arg name → Ansible parameter name (a valid variable: hyphens → underscores). */
export function paramName(argName: string): string {
  return argName.replace(/-/g, '_');
}

/** Per-module output file name. */
export function moduleFilename(leafName: string): string {
  return `${moduleName(leafName)}.py`;
}

// ── Python-literal rendering (for the generated argument_spec / ARGS) ──────────

/**
 * Render a JSON-ish value as a Python literal (True/False/None, not the JSON
 * spellings), pretty-printed for dicts/lists so the generated module reads
 * naturally and diffs stably.
 */
function py(value: unknown, indent = 0): string {
  const pad = '    '.repeat(indent);
  const pad1 = '    '.repeat(indent + 1);
  if (value === null || value === undefined) return 'None';
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  if (typeof value === 'number') return String(value);
  if (typeof value === 'string') return '"' + value.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    const items = value.map((v) => pad1 + py(v, indent + 1)).join(',\n');
    return '[\n' + items + ',\n' + pad + ']';
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return '{}';
    const items = entries.map(([k, v]) => `${pad1}${py(k)}: ${py(v, indent + 1)}`).join(',\n');
    return '{\n' + items + ',\n' + pad + '}';
  }
  return py(String(value));
}

// ── YAML-scalar rendering (for the DOCUMENTATION / EXAMPLES / RETURN blocks) ───

/**
 * A YAML scalar. Strings are double-quoted+escaped so a description containing a
 * colon or `#` can't break the doc block; bools/numbers render bare.
 */
function yamlScalar(value: unknown): string {
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') return String(value);
  return '"' + String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
}

// ── argument_spec + argv metadata ─────────────────────────────────────────────

/** One arg → its Ansible `argument_spec` entry. */
export function argSpecEntry(arg: ArgSpec): Record<string, unknown> {
  const argType = arg.type ?? 'str';
  const entry: Record<string, unknown> = { type: SPEC_TYPE[argType] ?? 'str' };
  if (argType === 'rest') entry['elements'] = 'str';
  if (argType === 'password') entry['no_log'] = true;
  if (arg.options && arg.options.length) entry['choices'] = arg.options.map((o) => String(o));
  if (arg.required) entry['required'] = true;
  const def = arg.default;
  if (def !== undefined && def !== null && argType !== 'rest') entry['default'] = def;
  return entry;
}

type Kind = 'secret' | 'rest' | 'flag' | 'positional' | 'named';

function argKind(arg: ArgSpec): Kind {
  const t = arg.type ?? 'str';
  if (t === 'password') return 'secret';
  if (t === 'rest') return 'rest';
  if (t === 'flag') return 'flag';
  if (arg.position !== undefined && arg.position !== null) return 'positional';
  return 'named';
}

/** The `ARGS` table the generated module loops over: [param, kind, cli, position, secretEnv]. */
export function argsMetadata(runnable: string, args: Record<string, ArgSpec>): unknown[][] {
  const rows: unknown[][] = [];
  for (const [name, arg] of Object.entries(args)) {
    const kind = argKind(arg);
    rows.push([
      paramName(name),
      kind,
      `--${name}`,
      arg.position ?? null,
      kind === 'secret' ? secretEnvVar(runnable, name) : null,
    ]);
  }
  return rows;
}

// ── documentation blocks ──────────────────────────────────────────────────────

/** Ansible `short_description`: a one-line summary, no trailing period. */
export function shortDescription(description: string): string {
  const s = (description || '').trim();
  const first = s ? s.split('\n')[0] : '';
  return first.replace(/\.+$/, '') || 'Run the runspec runnable';
}

/** The `DOCUMENTATION` YAML body (drives the AAP survey form + ansible-doc). */
export function renderDocumentation(mod: string, description: string, args: Record<string, ArgSpec>): string {
  const lines = [
    `module: ${mod}`,
    `short_description: ${yamlScalar(shortDescription(description))}`,
    'description:',
    `  - ${yamlScalar(description || shortDescription(description))}`,
  ];
  if (Object.keys(args).length) {
    lines.push('options:');
    for (const [name, arg] of Object.entries(args)) {
      const entry = argSpecEntry(arg);
      lines.push(`  ${paramName(name)}:`);
      lines.push(`    description: ${yamlScalar(arg.description || name)}`);
      lines.push(`    required: ${entry['required'] ? 'true' : 'false'}`);
      lines.push(`    type: ${entry['type']}`);
      if (entry['type'] === 'list') lines.push('    elements: str');
      if (entry['choices']) {
        lines.push('    choices: [' + (entry['choices'] as string[]).map(yamlScalar).join(', ') + ']');
      }
      if ('default' in entry) lines.push(`    default: ${yamlScalar(entry['default'])}`);
    }
  }
  return lines.join('\n');
}

/**
 * A minimal `EXAMPLES` YAML body — the module with its required args. `password`
 * args are omitted: a secret belongs in Vault / the environment, not inlined.
 */
export function renderExamples(mod: string, args: Record<string, ArgSpec>): string {
  const shown = Object.entries(args).filter(([, arg]) => arg.type !== 'password');
  const required = shown.filter(([, arg]) => argSpecEntry(arg)['required']);
  const sample = required.length ? required : shown;
  const lines = [`- name: Run ${mod}`, `  ${mod}:`];
  for (const [name, arg] of sample.slice(0, 4)) {
    const val = arg.default !== undefined && arg.default !== null ? arg.default : 'value';
    lines.push(`    ${paramName(name)}: ${yamlScalar(val)}`);
  }
  if (sample.length === 0) lines[lines.length - 1] = `  ${mod}: {}`;
  return lines.join('\n');
}

const RETURN_BLOCK = `changed:
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
  returned: when stdout is a JSON object`;

// ── module body (constant boilerplate — verbatim Python source) ────────────────

const BODY = `import json
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
`;

// ── module assembly ───────────────────────────────────────────────────────────

/** The full Ansible module `.py` source for one leaf command. */
export function renderModule(runnable: string, leaf: Leaf, invocation: string): string {
  const mod = moduleName(leaf.name);
  const args = leaf.args;
  const description = leaf.description || '';

  const argumentSpec: Record<string, unknown> = {};
  for (const [name, arg] of Object.entries(args)) {
    argumentSpec[paramName(name)] = argSpecEntry(arg);
  }
  const argsMeta = argsMetadata(runnable, args);

  return [
    '#!/usr/bin/python\n',
    '# -*- coding: utf-8 -*-\n',
    '# Generated by `runspec emit --ansible` from runspec.toml. Do not edit by hand.\n',
    'from __future__ import annotations\n\n',
    "DOCUMENTATION = r'''\n" + renderDocumentation(mod, description, args) + "\n'''\n\n",
    "EXAMPLES = r'''\n" + renderExamples(mod, args) + "\n'''\n\n",
    "RETURN = r'''\n" + RETURN_BLOCK + "\n'''\n\n",
    `RUNNABLE = ${py(invocation)}\n`,
    `COMMAND_PATH = ${py(leaf.commandPath)}\n`,
    '# Each row: [param, kind, cli_flag, position, secret_env]\n',
    `ARGS = ${py(argsMeta)}\n`,
    `ARGUMENT_SPEC = ${py(argumentSpec)}\n\n\n`,
    BODY,
  ].join('');
}

/** All Ansible modules for one runnable (one per leaf), keyed by file name. */
export function renderRunnable(runnable: string, script: ScriptSpec, invocation: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const leaf of expandLeaves(runnable, script)) {
    out[moduleFilename(leaf.name)] = renderModule(runnable, leaf, invocation);
  }
  return out;
}

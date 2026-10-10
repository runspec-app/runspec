/**
 * rundeckgen.ts — Render Rundeck job definitions (JSON) from runspec runnables.
 *
 * Pure module (no filesystem side effects) so its output is deterministic —
 * identical input yields byte-identical JSON, which keeps the
 * `runspec emit --rundeck --check` drift diff stable. The CLI (`cli.cmdEmit`)
 * owns discovery and disk I/O; this module owns the render.
 *
 * Ports `runspec/rundeckgen.py`. One Rundeck job per **leaf command** (a
 * runnable with subcommands flattens to one job per fully-pathed leaf, like
 * `runspec serve`). Every job runs a single Bash **script step** that reads
 * each option's `@option.<name>@` token, through a quoted heredoc, into its
 * runspec env var `RUNSPEC_<RUNNABLE>_ARG_<NAME>` and runs the binary with no
 * arg values on its command line (they would show in `ps`). It doesn't read
 * `RD_OPTION_*` variables (those need `AcceptEnv RD_*` on every node's sshd).
 *
 * Safe on Rundeck before 5.20, which substitutes option values unescaped: the
 * shell never parses a value, and a line-count guard refuses a value with a
 * line break (the only way out of its heredoc). Rundeck writes the substituted
 * script, secrets included, to a temp file on the node; the script's first
 * command makes that file readable by its owner only.
 */

import { createHash } from 'crypto';
import { ArgSpec, ScriptSpec } from './models';
import { Leaf, expandLeaves, secretEnvVar } from './emitutil';

// runspec type → a validating regex for the plain text Rundeck option (Rundeck
// has no native int/float option type).
const NUMERIC_REGEX: Record<string, string> = {
  int: '^-?\\d+$',
  float: '^-?\\d+(\\.\\d+)?$',
};

// ── stable UUIDv5 (RFC 4122) ──────────────────────────────────────────────────
// Rundeck jobs carry a stable UUID so re-emitting + re-importing updates the
// same job instead of duplicating. Deterministic (never crypto.randomUUID).

function uuid5(namespace: string, name: string): string {
  const nsBytes = Buffer.from(namespace.replace(/-/g, ''), 'hex');
  const digest = createHash('sha1').update(nsBytes).update(Buffer.from(name, 'utf-8')).digest();
  const bytes = Buffer.from(digest.subarray(0, 16));
  bytes[6] = (bytes[6] & 0x0f) | 0x50; // version 5
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // RFC 4122 variant
  const h = bytes.toString('hex');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

const NAMESPACE_URL = '6ba7b811-9dad-11d1-80b4-00c04fd430c8';
const UUID_NAMESPACE = uuid5(NAMESPACE_URL, 'https://runspec.app/rundeck');

// ── name helpers ──────────────────────────────────────────────────────────────

/** Per-job output file name. */
export function jobFilename(jobName: string): string {
  return `${jobName}.json`;
}

// ── option rendering ──────────────────────────────────────────────────────────

function optionDescription(arg: ArgSpec): string {
  const parts: string[] = [];
  if (arg.description) parts.push(String(arg.description));
  const notes: string[] = [];
  if (arg.type === 'rest') notes.push('space-separated; passed to the runnable after --');
  if (arg.position !== undefined && arg.position !== null) notes.push(`positional #${arg.position}`);
  if (arg.range) notes.push(`range ${arg.range[0]}–${arg.range[1]}`);
  if (arg.pattern) notes.push(`pattern ${arg.pattern}`);
  if (notes.length) parts.push('(' + notes.join('; ') + ')');
  return parts.join(' ');
}

/**
 * Render one runspec arg as a Rundeck job option.
 *
 * - `password` → a **secure** option (`secure, valueExposed`): entered at run
 *   time, kept out of the job log, reaching the script only through its
 *   `@option.<name>@` token; never carries a default `value`.
 * - `flag` → text option constrained to `["true","false"]` (`enforced`).
 * - `choice`/`options` → text option constrained to the choices (`enforced`).
 * - `int`/`float` → text option with a validating `regex`.
 * - everything else → plain text option.
 */
export function renderOption(name: string, arg: ArgSpec): Record<string, unknown> {
  const opt: Record<string, unknown> = { name };
  const desc = optionDescription(arg);
  if (desc) opt['description'] = desc;

  const argType = arg.type ?? 'str';

  if (argType === 'password') {
    opt['secure'] = true;
    opt['valueExposed'] = true;
    if (arg.required) opt['required'] = true;
    return opt;
  }

  if (arg.required) opt['required'] = true;

  const def = arg.default;

  if (argType === 'flag') {
    opt['values'] = ['true', 'false'];
    opt['enforced'] = true;
    opt['value'] = def === true ? 'true' : 'false';
  } else if (arg.options && arg.options.length) {
    opt['values'] = arg.options.map((o) => String(o));
    opt['enforced'] = true;
    if (def !== undefined && def !== null) opt['value'] = String(def);
  } else {
    if (def !== undefined && def !== null && argType !== 'rest') opt['value'] = String(def);
    const regex = NUMERIC_REGEX[argType];
    if (regex) opt['regex'] = regex;
  }

  return opt;
}

// ── script step ───────────────────────────────────────────────────────────────

// The SPEC "env prelude" (shell) and "Python prelude" — byte-identical to
// `runspec.become.ENV_PRELUDE` / `PY_ENV_PRELUDE` and spec/SPEC.md. They read
// the env frame from stdin, export each variable and exec the command.
export const ENV_PRELUDE =
  'while IFS= read -r l; do [ "$l" = . ] && break; n=${l%%=*}; v=$(printf "%b" "${l#*=}X"); export "$n=${v%X}"; done; exec "$@"';

export const PY_ENV_PRELUDE = [
  'import os,sys',
  'l=bytes()',
  'while 1:',
  ' c=os.read(0,1)',
  ' if c and c!=bytes([10]):',
  '  l+=c;continue',
  ' if l==bytes([46]) or not l and not c:break',
  ' if l:n,s,v=l.partition(bytes([61]));os.environ[n.decode()]=bytes(int(v[i+2:i+5],8) for i in range(0,len(v),5)).decode()',
  ' if not c:break',
  ' l=bytes()',
  'os.execvp(sys.argv[1],sys.argv[1:])',
].join('\n');

// Bash helper that writes the SPEC "env frame" for the named shell variables to
// stdout: one `NAME=\0ooo…` line per non-empty variable (every byte of the value
// as an octal escape), then a `.` line. Only shell builtins see a value
// (`printf`), and `od`/`tr`/`sed` read it from a pipe, so no value reaches any
// process's argv. The env prelude on the far side of `sudo` reads it back.
export const FRAME_FUNC = [
  'rs_frame() {',
  '  local rs_var rs_val',
  '  for rs_var in "$@"; do',
  '    rs_val=${!rs_var}',
  '    [ -n "$rs_val" ] || continue',
  `    printf '%s=%s\\n' "$rs_var" "$(printf '%s' "$rs_val" | od -An -v -to1 | tr -d ' \\n' | sed 's/.../\\\\0&/g')"`,
  '  done',
  "  printf '.\\n'",
  '}',
].join('\n');

// Delimiter of the per-value heredocs. Each body line starts with `x`, so no
// substituted value can turn its line into this delimiter.
const VALUE_EOF = 'RUNSPEC_VALUE';

// The script's third line: count the script file's own non-blank lines and
// refuse to run unless the count is the one the emitter wrote. A Rundeck option
// value is substituted into the script text before it reaches the node, and the
// only way one can escape its heredoc is a line break, which adds a line. The
// guard runs before Bash reads any value line. Blank lines are not counted, so
// a trailing newline Rundeck adds (or an empty line in a value) changes nothing;
// an unreadable script file fails closed.
const GUARD_COUNT = '@RS_LINES@';
const GUARD =
  `[ "$(awk 'NF { n++ } END { print n + 0 }' "\${BASH_SOURCE[0]:-$0}")" = ${GUARD_COUNT} ] || ` +
  "{ echo 'runspec: a Rundeck option value contains a line break; refusing to run' >&2; exit 64; }";

/** Join the script and fill in the guard's expected non-blank line count. */
function finish(lines: string[]): string {
  const script = lines.join('\n');
  const count = script.split('\n').filter((line) => line.trim()).length;
  return script.replace(GUARD_COUNT, String(count));
}

const RUN_AS = /^[A-Za-z0-9_.][A-Za-z0-9_.-]*$/;

// `run_as` value resolved on the node to the owner of the venv (the core's
// default, SPEC "run_as"): never root, and no sudo when already that account.
const OWNER = '@owner';

// The script's second line: make the script file readable by its owner only.
// Rundeck copies it to the node's temp dir with the default umask, and leaves
// it there when a killed job's cleanup fails, values included.
const PRIVATE =
  'chmod 700 -- "${BASH_SOURCE[0]:-$0}" || ' +
  "{ echo 'runspec: could not make the job script private to its owner; refusing to run' >&2; exit 64; }";

// Who ran a failed check, and where, for the not-found message.
const WHERE = '"$(id -un)" "${HOSTNAME:-this node}"';

/** Option name → the `@option.<name>@` token Rundeck substitutes into an inline script. */
export function rdOptionToken(name: string): string {
  return `@option.${name}@`;
}

/** POSIX shell quoting, identical to Python's `shlex.quote`. */
export function shellQuote(s: string): string {
  if (s === '') return "''";
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(s)) return s;
  return "'" + s.replace(/'/g, `'"'"'`) + "'";
}

/** `{ printf …; exit 127; }` naming the runnable, where it was looked for, and who looked. */
function notFound(runnable: string, where: string): string {
  return (
    `{ printf 'runspec: %s not found %s (checked as %s on %s)\\n' ` +
    `${shellQuote(runnable)} ${shellQuote(where)} ${WHERE} >&2; exit 127; }`
  );
}

/**
 * Lines that stop the job with a clear message when the runnable isn't where
 * the job expects it: `<venv>/bin/<runnable>`, or on the Rundeck user's PATH
 * without a venv. A venv `bin/` this account can't search is left to the run
 * itself; with a fixed `run_as` and no venv there is no check (sudo's PATH).
 */
function existsCheck(runnable: string, invocation: string, venv: string | null | undefined, runAs: string | undefined): string[] {
  if (venv) {
    const binDir = shellQuote(`${String(venv).replace(/\/+$/, '')}/bin`);
    return [
      `[ -e ${shellQuote(invocation)} ] || { [ -d ${binDir} ] && [ ! -x ${binDir} ]; } || ` +
        notFound(runnable, `at ${invocation}`),
    ];
  }
  if (runAs && runAs !== OWNER) return [];
  return [`rs_bin=$(command -v ${shellQuote(invocation)}) || ` + notFound(runnable, 'on PATH')];
}

/** Export the non-empty values and exec the binary as this account. */
function direct(envVars: ReadonlyArray<readonly [string, string]>, cmd: string, indent = ''): string[] {
  const lines: string[] = [];
  if (envVars.length) {
    lines.push(`${indent}for rs_var in ${envVars.map(([v]) => v).join(' ')}; do`);
    lines.push(`${indent}  if [ -n "\${!rs_var}" ]; then export "$rs_var"; else unset "$rs_var"; fi`);
    lines.push(`${indent}done`);
  }
  lines.push(`${indent}exec ${cmd}`);
  return lines;
}

/**
 * Build the Bash script that runs the runnable with the job's option values.
 *
 * The script first makes its own file private to its owner. Every value —
 * secret or not — reaches the runnable on runspec's env channel
 * (`RUNSPEC_<RUNNABLE>_ARG_<NAME>`), never on a command line. Each
 * `@option.<name>@` token sits alone (behind an `x`) inside a quoted heredoc
 * that `read` takes into the variable, so the shell never parses a value; the
 * guard on line 3 refuses a value with a line break. A runnable that isn't
 * where the job expects it stops the job with exit 127 and a clear message.
 *
 * `runAs`: unset/`""` runs as the Rundeck user; an account name, or `"@owner"`
 * (the venv's owner, resolved on the node, never root), runs through
 * `sudo -n -u` unless the Rundeck user already is that account — with the
 * values as the SPEC env frame on the child's stdin, never in argv or the sudo
 * log.
 */
export function renderScript(
  runnable: string,
  commandPath: string[],
  args: Record<string, ArgSpec>,
  invocation: string,
  runAs?: string | null,
  venv?: string | null,
): string {
  const envVars = Object.keys(args).map((name) => [secretEnvVar(runnable, name), name] as const);
  const names = envVars.map(([v]) => v).join(' ');

  const lines = ['#!/bin/bash', PRIVATE, GUARD, 'set -e'];
  for (const [v, name] of envVars) {
    lines.push(`IFS= read -r ${v} <<'${VALUE_EOF}'`);
    lines.push(`x${rdOptionToken(name)}`);
    lines.push(VALUE_EOF);
    lines.push(`${v}=\${${v}#x}`);
  }

  const target = runAs || undefined;
  lines.push(...existsCheck(runnable, invocation, venv, target));

  let cmd = [invocation, ...commandPath].map(shellQuote).join(' ');
  if (!target) {
    lines.push(...direct(envVars, cmd));
    return finish(lines);
  }

  if (target === OWNER) {
    const root = venv ? shellQuote(String(venv).replace(/\/+$/, '')) : '"$rs_venv"';
    if (!venv) {
      // The venv is the one PATH found the binary in; run that same file
      // (sudo's own PATH may not reach it).
      lines.push('rs_venv=${rs_bin%/*}; rs_venv=${rs_venv%/*}');
      cmd = ['"$rs_bin"', ...commandPath.map(shellQuote)].join(' ');
    }
    lines.push(`rs_user=$(stat -c %U -- ${root})`);
    lines.push(`[ "$(stat -c %u -- ${root})" != 0 ] || rs_user=`);
    lines.push('if [ -z "$rs_user" ] || [ "$rs_user" = "$(id -un)" ]; then');
  } else if (RUN_AS.test(target)) {
    lines.push(`rs_user=${target}`);
    lines.push('if [ "$rs_user" = "$(id -un)" ]; then');
  } else {
    throw new Error(`run_as '${target}' is not a valid account name`);
  }
  lines.push(...direct(envVars, cmd, '  '));
  lines.push('fi');

  const become = 'sudo -n -u "$rs_user"';
  if (!envVars.length) {
    lines.push(`exec ${become} ${cmd}`);
    return finish(lines);
  }

  lines.push(`RS_PRELUDE='${venv ? PY_ENV_PRELUDE : ENV_PRELUDE}'`);
  lines.push(FRAME_FUNC);
  const prelude = venv
    ? [shellQuote(`${String(venv).replace(/\/+$/, '')}/bin/python`), '-c', '"$RS_PRELUDE"']
    : ['sh', '-c', '"$RS_PRELUDE"', 'sh'];
  lines.push(`rs_frame ${names} | ${become} ${[...prelude, cmd].join(' ')}`);
  return finish(lines);
}

// ── notifications ─────────────────────────────────────────────────────────────

const NOTIFY_TRIGGERS = ['onsuccess', 'onfailure', 'onstart'] as const;

/**
 * Render a `meta.rundeck.notifications` table into a Rundeck `notification`
 * block. Each trigger may carry `email` ({recipients, subject?, attachLog?})
 * and/or `webhook`/`url` ({urls}).
 */
export function renderNotifications(spec: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const trigger of NOTIFY_TRIGGERS) {
    const cfg = spec[trigger];
    if (!cfg || typeof cfg !== 'object') continue;
    const c = cfg as Record<string, unknown>;
    const block: Record<string, unknown> = {};

    const email = c['email'];
    if (typeof email === 'string') {
      block['email'] = { recipients: email };
    } else if (email && typeof email === 'object' && (email as Record<string, unknown>)['recipients']) {
      const em = email as Record<string, unknown>;
      const e: Record<string, unknown> = { recipients: em['recipients'] };
      if (em['subject']) e['subject'] = em['subject'];
      if (em['attachLog']) e['attachLog'] = true;
      block['email'] = e;
    }

    const urls = c['webhook'] ?? c['url'] ?? c['urls'];
    if (typeof urls === 'string') {
      block['webhook'] = { urls };
    } else if (urls && typeof urls === 'object' && (urls as Record<string, unknown>)['urls']) {
      block['webhook'] = { urls: (urls as Record<string, unknown>)['urls'] };
    }

    if (Object.keys(block).length) out[trigger] = block;
  }
  return out;
}

// ── settings resolution ───────────────────────────────────────────────────────

function firstDefined<T>(...values: T[]): T | undefined {
  for (const v of values) if (v !== undefined && v !== null) return v;
  return undefined;
}

/** The emit CLI flags that override per-runnable Rundeck settings. */
export interface RundeckCliOpts {
  venv?: string;
  group?: string;
  nodeFilter?: string;
  project?: string;
  runAs?: string;
}

export interface RundeckSettings {
  invocation: string;
  venv: unknown;
  runAs?: string;
  group: string;
  project: string;
  nodeFilter?: string;
  orchestrator: string;
  threadcount: number;
  schedule?: unknown;
  notifications?: Record<string, unknown>;
  loglevel: string;
  strategy: string;
  keepgoing: boolean;
  timeout?: unknown;
  retry?: unknown;
}

/**
 * Resolve per-runnable Rundeck settings. Precedence: CLI flag >
 * `[<runnable>.meta.rundeck]` > `[config.meta.rundeck]` > built-in default.
 * `run_as` falls back to `defaultRunAs` — the runnable's own `run_as`, else its
 * `[config] run_as` — and, when neither is set, to `"@owner"` like the core.
 */
export function mergeSettings(
  runnable: string,
  cliOpts: RundeckCliOpts,
  configMeta: Record<string, unknown>,
  runnableMeta: Record<string, unknown>,
  defaultRunAs?: unknown,
): RundeckSettings {
  const venv = firstDefined(cliOpts.venv, runnableMeta['venv'], configMeta['venv']);
  const invocation = venv ? `${String(venv).replace(/\/+$/, '')}/bin/${runnable}` : runnable;

  const nodeFilter = firstDefined(cliOpts.nodeFilter, runnableMeta['node_filter'], configMeta['node_filter']) as
    | string
    | undefined;

  let orchestrator = firstDefined(runnableMeta['orchestrator'], configMeta['orchestrator']) as string | undefined;
  if (orchestrator === undefined) orchestrator = nodeFilter ? 'subset' : 'none';

  const notifications = firstDefined(runnableMeta['notifications'], configMeta['notifications']) as
    | Record<string, unknown>
    | undefined;

  const runAs = firstDefined(cliOpts.runAs, runnableMeta['run_as'], configMeta['run_as'], emitRunAs(defaultRunAs)) as
    | string
    | undefined;

  return {
    invocation,
    venv,
    runAs: runAs || undefined,
    group: (firstDefined(cliOpts.group, runnableMeta['group'], configMeta['group'], 'runspec') as string),
    project: (firstDefined(cliOpts.project, configMeta['project'], 'runspec') as string),
    nodeFilter,
    orchestrator,
    threadcount: Number(firstDefined(runnableMeta['threadcount'], configMeta['threadcount'], 1)),
    schedule: firstDefined(runnableMeta['schedule'], configMeta['schedule']),
    notifications,
    loglevel: (firstDefined(runnableMeta['loglevel'], configMeta['loglevel'], 'INFO') as string),
    strategy: (firstDefined(runnableMeta['strategy'], configMeta['strategy'], 'node-first') as string),
    keepgoing: Boolean(firstDefined(runnableMeta['keepgoing'], configMeta['keepgoing'], false)),
    timeout: firstDefined(runnableMeta['timeout'], configMeta['timeout']),
    retry: firstDefined(runnableMeta['retry'], configMeta['retry']),
  };
}

/**
 * A runnable's `run_as` as the job uses it: an account name, `"@owner"` (also
 * when unset — the core default), or `undefined` for the `""` opt-out and the
 * forms a job can't resolve (`$ENV`, a per-host table).
 */
export function emitRunAs(value: unknown): string | undefined {
  if (value === undefined || value === null) return OWNER;
  if (typeof value === 'string' && value && (value === OWNER || !'@$'.includes(value[0]))) return value;
  return undefined;
}

// ── job + file assembly ───────────────────────────────────────────────────────

/** Assemble one Rundeck job object from a leaf command and resolved settings. */
export function renderJob(runnable: string, leaf: Leaf, settings: RundeckSettings): Record<string, unknown> {
  const jobName = leaf.name;
  const args = leaf.args;
  const group = settings.group;

  const venv = settings.venv ? String(settings.venv) : undefined;
  const script = renderScript(runnable, leaf.commandPath, args, settings.invocation, settings.runAs, venv);

  const job: Record<string, unknown> = {
    name: jobName,
    description: leaf.description || '',
    group,
    loglevel: settings.loglevel,
    uuid: uuid5(UUID_NAMESPACE, `${settings.project}/${group}/${jobName}`),
    executionEnabled: true,
    sequence: {
      keepgoing: settings.keepgoing,
      strategy: settings.strategy,
      commands: [{ script, scriptInterpreter: '/bin/bash' }],
    },
  };

  const options = Object.entries(args).map(([name, arg]) => renderOption(name, arg));
  if (options.length) job['options'] = options;

  if (settings.nodeFilter) {
    job['nodefilters'] = {
      filter: settings.nodeFilter,
      dispatch: { threadcount: settings.threadcount, keepgoing: settings.keepgoing },
    };
    if (settings.orchestrator === 'subset') {
      job['orchestrator'] = { type: 'subset', configuration: { count: '1' } };
    }
  }

  if (settings.schedule) {
    job['schedule'] = { crontab: String(settings.schedule) };
    job['scheduleEnabled'] = true;
  }

  if (settings.notifications) {
    const notification = renderNotifications(settings.notifications);
    if (Object.keys(notification).length) job['notification'] = notification;
  }

  if (settings.timeout !== undefined && settings.timeout !== null) job['timeout'] = String(settings.timeout);
  if (settings.retry !== undefined && settings.retry !== null) job['retry'] = String(settings.retry);

  return job;
}

/** All Rundeck job objects for one runnable (one per leaf command). */
export function renderRunnable(runnable: string, script: ScriptSpec, settings: RundeckSettings): Record<string, unknown>[] {
  return expandLeaves(runnable, script).map((leaf) => renderJob(runnable, leaf, settings));
}

// ── deterministic serialization ────────────────────────────────────────────────

function sortValue(v: unknown): unknown {
  if (Array.isArray(v)) return v.map(sortValue);
  if (v && typeof v === 'object') {
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(v as Record<string, unknown>).sort()) {
      out[k] = sortValue((v as Record<string, unknown>)[k]);
    }
    return out;
  }
  return v;
}

/**
 * Serialize a list of job objects as a Rundeck job-definition JSON array.
 * Sorted keys + trailing newline keep output byte-stable for `--check` (list
 * order — e.g. the `options` array — is preserved; only object keys are sorted).
 */
export function dumps(jobs: Record<string, unknown>[]): string {
  return JSON.stringify(sortValue(jobs), null, 2) + '\n';
}

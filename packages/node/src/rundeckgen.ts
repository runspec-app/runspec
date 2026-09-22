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
 * `runspec serve`). Every job runs a single Bash **script step** that reads each
 * option from its `RD_OPTION_<NAME>` environment variable — that avoids script
 * injection and lets a secret ride the env channel without ever landing on the
 * command line. Targets Rundeck 4.17.6.
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

/** Option name → the `RD_OPTION_<NAME>` env var Rundeck exposes it as. */
export function rdEnvVar(name: string): string {
  return 'RD_OPTION_' + name.replace(/[^A-Za-z0-9]/g, '_').toUpperCase();
}

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
 *   time, kept off the command line, exposed to the script only as
 *   `RD_OPTION_<NAME>`; never carries a default `value`.
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

type Category = 'secret' | 'rest' | 'flag' | 'positional' | 'named';

function argCategory(arg: ArgSpec): Category {
  const t = arg.type ?? 'str';
  if (t === 'password') return 'secret';
  if (t === 'rest') return 'rest';
  if (t === 'flag') return 'flag';
  if (arg.position !== undefined && arg.position !== null) return 'positional';
  return 'named';
}

/**
 * Build the Bash script that reconstructs the CLI invocation from options.
 * Values read from `RD_OPTION_*` env vars (defaulted to empty). Named args only
 * when non-empty; flags only when `"true"`; positionals in `position` order; a
 * `rest` arg word-split after a literal `--`. `password` args ride the runspec
 * secret channel (`RUNSPEC_<RUNNABLE>_ARG_<NAME>`), never the command line.
 */
export function renderScript(
  runnable: string,
  commandPath: string[],
  args: Record<string, ArgSpec>,
  invocation: string,
): string {
  const named: string[] = [];
  const flags: string[] = [];
  const positionals: Array<[string, ArgSpec]> = [];
  const rests: string[] = [];
  const secrets: string[] = [];

  for (const [name, arg] of Object.entries(args)) {
    const cat = argCategory(arg);
    const env = rdEnvVar(name);
    if (cat === 'named') {
      named.push(`if [ -n "\${${env}:-}" ]; then args+=(--${name} "\${${env}:-}"); fi`);
    } else if (cat === 'flag') {
      flags.push(`if [ "\${${env}:-}" = "true" ]; then args+=(--${name}); fi`);
    } else if (cat === 'positional') {
      positionals.push([name, arg]);
    } else if (cat === 'rest') {
      // Intentionally unquoted so the value word-splits into separate args.
      rests.push(`if [ -n "\${${env}:-}" ]; then args+=(-- \${${env}:-}); fi`);
    } else {
      secrets.push(`export ${secretEnvVar(runnable, name)}="\${${env}:-}"`);
    }
  }

  positionals.sort((a, b) => (a[1].position ?? 0) - (b[1].position ?? 0));
  const positionalLines = positionals.map(([name]) => {
    const env = rdEnvVar(name);
    return `if [ -n "\${${env}:-}" ]; then args+=("\${${env}:-}"); fi`;
  });

  const lines = ['#!/bin/bash', 'set -e', 'args=()', ...named, ...flags, ...positionalLines, ...rests, ...secrets];
  const invoke = [invocation, ...commandPath, '"${args[@]}"'].join(' ');
  lines.push(`exec ${invoke}`);
  return lines.join('\n');
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
}

export interface RundeckSettings {
  invocation: string;
  venv: unknown;
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
 */
export function mergeSettings(
  runnable: string,
  cliOpts: RundeckCliOpts,
  configMeta: Record<string, unknown>,
  runnableMeta: Record<string, unknown>,
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

  return {
    invocation,
    venv,
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

// ── job + file assembly ───────────────────────────────────────────────────────

/** Assemble one Rundeck job object from a leaf command and resolved settings. */
export function renderJob(runnable: string, leaf: Leaf, settings: RundeckSettings): Record<string, unknown> {
  const jobName = leaf.name;
  const args = leaf.args;
  const group = settings.group;

  const script = renderScript(runnable, leaf.commandPath, args, settings.invocation);

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

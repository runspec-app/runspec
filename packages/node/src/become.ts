/**
 * become.ts — run_as resolution and the enforce_run_as identity check.
 *
 * Mirrors the Python `runspec.become` module for the SPEC "Remote Execution"
 * semantics. Node does not build escalation commands (no `runspec serve`
 * sudo wrapping), so only the pieces the `enforce_run_as` gate needs are
 * ported: resolving a `run_as` spec to a concrete username for a host, and
 * comparing the resolved user to the current process's effective user.
 *
 * See spec/SPEC.md → "Remote Execution".
 */

import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

/**
 * Resolve a `run_as` spec to a concrete username for `hostname`.
 *
 * Forms (see SPEC): simple string, `$ENV_VAR`, or a table with `hosts` (exact
 * match) / `patterns` (full-match regex, first wins, insertion order) /
 * `default`. Returns `''` for "no privilege escalation".
 */
export function resolveRunAs(spec: unknown, hostname: string): string {
  if (spec === null || spec === undefined) return '';

  if (typeof spec === 'string') {
    if (spec.startsWith('$')) return process.env[spec.slice(1)] ?? '';
    return spec;
  }

  if (typeof spec === 'object') {
    const s = spec as Record<string, unknown>;

    const hosts = (s['hosts'] as Record<string, unknown>) ?? {};
    if (hostname in hosts) return String(hosts[hostname]);

    const patterns = (s['patterns'] as Record<string, unknown>) ?? {};
    for (const [pattern, user] of Object.entries(patterns)) {
      try {
        // Anchor both ends for re.fullmatch() semantics.
        if (new RegExp(`^(?:${pattern})$`).test(hostname)) return String(user);
      } catch {
        // Invalid regex — skip it (serve validates patterns up front).
      }
    }

    return String(s['default'] ?? '');
  }

  return '';
}

/** The reserved run_as value for "the account that owns this environment" —
 * the library default (SPEC → Remote Execution → run_as). */
export const OWNER = '@owner';

/**
 * The `run_as` spec that applies: the runnable's own, else the `[config]`
 * default, else `"@owner"`. `""` at either level is a real value (no
 * escalation), so only `undefined`/`null` (absent) falls through.
 */
export function effectiveRunAs(runnableValue: unknown, configValue?: unknown): unknown {
  if (runnableValue !== undefined && runnableValue !== null) return runnableValue;
  if (configValue !== undefined && configValue !== null) return configValue;
  return OWNER;
}

/**
 * The environment root for a runnable whose `runspec.toml` is at `configPath`:
 * the project root — the nearest ancestor holding a `package.json`, skipping
 * anything inside `node_modules` — else the config's own directory.
 */
export function environmentRoot(configPath: string): string {
  let dir = path.dirname(path.resolve(configPath));
  const start = dir;
  for (;;) {
    if (!dir.split(path.sep).includes('node_modules') && fs.existsSync(path.join(dir, 'package.json'))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return start;
    dir = parent;
  }
}

function uidName(uid: number): string {
  try {
    return execFileSync('id', ['-un', String(uid)], { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim() || String(uid);
  } catch {
    return String(uid);
  }
}

/**
 * The account that owns `envRoot`, or `''` when there is none to run as: a
 * root-owned environment (`"@owner"` never escalates to root), an unreadable
 * root, or a platform with no POSIX uid model (Windows).
 */
export function environmentOwner(envRoot: string): string {
  if (!process.geteuid) return '';
  let uid: number;
  try {
    uid = fs.statSync(envRoot).uid;
  } catch {
    return '';
  }
  return uid === 0 ? '' : uidName(uid);
}

/** A resolved `run_as` string with `"@owner"` replaced by the environment's owner. */
export function resolveAccount(runAs: string, envRoot: string): string {
  return runAs === OWNER ? environmentOwner(envRoot) : runAs;
}

/**
 * Check that the current process is running as `runAsUser`.
 *
 * Returns `null` when the identity matches *or* the check cannot be performed —
 * an empty target (no escalation intended, so no identity to assert) or a
 * platform with no POSIX uid model (Windows, where `process.geteuid` is
 * undefined). Returns a human-readable detail string describing the mismatch
 * otherwise.
 *
 * The target is resolved to a uid (a numeric string is taken as a uid directly,
 * otherwise it is looked up with `id`) and compared to the effective uid, so
 * account aliases that share a uid are treated as equal. An executor escalates
 * *before* the process starts, so under one the effective user already matches
 * and this passes silently; it only fires on the direct/import path.
 */
export function verifyRunAsIdentity(runAsUser: string): string | null {
  if (!runAsUser) return null;

  const geteuid = process.geteuid;
  if (!geteuid) return null; // no POSIX uid model (Windows) — nothing to compare
  const currentUid = geteuid.call(process);

  let targetUid: number;
  try {
    if (/^\d+$/.test(runAsUser)) {
      // A numeric account (explicit, or an environment owner with no user
      // database entry) is compared as a uid — it needn't have a name.
      targetUid = parseInt(runAsUser, 10);
    } else {
      const out = execFileSync('id', ['-u', runAsUser], { stdio: ['ignore', 'pipe', 'ignore'] });
      targetUid = parseInt(out.toString().trim(), 10);
    }
  } catch {
    return `run_as user '${runAsUser}' does not exist on this host`;
  }

  if (currentUid === targetUid) return null;

  const currentName = uidName(currentUid);

  return `the current process runs as '${currentName}' (uid ${currentUid}), not '${runAsUser}' (uid ${targetUid})`;
}

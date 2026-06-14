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
      targetUid = parseInt(runAsUser, 10);
      // Confirm the uid maps to a real account; throws if it does not.
      execFileSync('id', ['-un', String(targetUid)], { stdio: ['ignore', 'ignore', 'ignore'] });
    } else {
      const out = execFileSync('id', ['-u', runAsUser], { stdio: ['ignore', 'pipe', 'ignore'] });
      targetUid = parseInt(out.toString().trim(), 10);
    }
  } catch {
    return `run_as user '${runAsUser}' does not exist on this host`;
  }

  if (currentUid === targetUid) return null;

  let currentName = String(currentUid);
  try {
    currentName =
      execFileSync('id', ['-un', String(currentUid)], { stdio: ['ignore', 'pipe', 'ignore'] })
        .toString()
        .trim() || String(currentUid);
  } catch {
    // Keep the numeric uid as the name.
  }

  return `the current process runs as '${currentName}' (uid ${currentUid}), not '${runAsUser}' (uid ${targetUid})`;
}

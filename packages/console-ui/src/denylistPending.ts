/**
 * Pure helpers for the kill switch's pending blocks (hosts "Disable all"
 * couldn't reach). Kept apart from the lazy-loaded DenylistModal so App can
 * apply pending blocks on reconnect without loading the window.
 */

/** Did a `runspec deny` call succeed on every venv it tried? Reads the
 *  summary header "runspec deny all — 2/2 venv(s) ok across 1 host(s)".
 *  Pure, for tests. */
export function denyAppliedEverywhere(summary: string): boolean {
  const m = summary.match(/(\d+)\/(\d+) venv\(s\) ok/)
  return !!m && Number(m[2]) > 0 && m[1] === m[2]
}


/** Shell-style glob match (`*`, `?`, `[…]`) — the same semantics as the
 *  core's `fnmatch` for the patterns a rule holds, case-sensitive like POSIX. */
export function globMatch(pattern: string, value: string): boolean {
  let re = ''
  for (let i = 0; i < pattern.length; i++) {
    const ch = pattern[i]
    if (ch === '*') re += '.*'
    else if (ch === '?') re += '.'
    else if (ch === '[') {
      const end = pattern.indexOf(']', i + 1)
      if (end === -1) { re += '\\['; continue }
      let body = pattern.slice(i + 1, end)
      if (body.startsWith('!')) body = '^' + body.slice(1)
      re += `[${body.replace(/\\/g, '\\\\')}]`
      i = end
    } else re += ch.replace(/[.+^${}()|\\]/g, '\\$&')
  }
  return new RegExp(`^${re}$`).test(value)
}

/** Which scheduler daemons a restart would leave stopped: the denylist on
 *  their venv is enforcing and either disables everything or has a rule whose
 *  runnables match `runspec-scheduler` (a rule limited to some users or hosts
 *  counts too, so this can over-warn, never under-warn). A row that couldn't
 *  be read counts as not blocked. Pure, for tests. */
export function schedulerBlocked(
  rows: { host: string; venv: string; ok: boolean; mode?: string; disabled?: boolean; rules?: { runnables: string[] }[] }[],
  daemons: { host: string; venv: string }[],
): string[] {
  const blocked = (r: (typeof rows)[number]) =>
    r.ok && (r.mode ?? 'enforce') === 'enforce' &&
    (!!r.disabled || (r.rules ?? []).some(rule => (rule.runnables.length ? rule.runnables : ['*']).some(g => globMatch(g, 'runspec-scheduler'))))
  return daemons
    .filter(dm => rows.some(r => r.host === dm.host && r.venv === dm.venv && blocked(r)))
    .map(dm => `${dm.host} / ${dm.venv}`)
}

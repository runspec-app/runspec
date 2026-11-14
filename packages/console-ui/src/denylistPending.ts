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


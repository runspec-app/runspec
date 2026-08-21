// toolVisibility.ts — client-side mirror of the backend [tool_visibility] matcher
// (runspec_console.bridge_utils.runnable_hidden). Used by the Forms view to hide
// `hidden_from_all` runnables and by the Settings → Agent tools tab to show each
// runnable's effective visibility. The backend is the authority for the agent
// toolset + catalogs; this is a display-only convenience.

// Convert a shell glob (`*`, `?`) to an anchored RegExp. Everything else is
// escaped so a pattern like `outlook-*` matches literally apart from the star.
function globToRe(glob: string): RegExp {
  let re = ''
  for (const ch of glob) {
    if (ch === '*') re += '.*'
    else if (ch === '?') re += '.'
    else re += ch.replace(/[.+^${}()|[\]\\]/g, '\\$&')
  }
  return new RegExp(`^${re}$`)
}

// True if a runnable matches any hide pattern. A `group:`/`host:` prefix matches
// the runnable's group/host instead of its name. Empty/blank patterns hide
// nothing (hiding is opt-in per pattern — the inverse of the core denylist).
export function runnableHidden(
  name: string,
  group: string | undefined,
  host: string | undefined,
  patterns: string[] | undefined | null,
): boolean {
  if (!patterns || patterns.length === 0) return false
  for (const raw of patterns) {
    const pat = (raw ?? '').trim()
    if (!pat) continue
    let target: string
    let glob: string
    if (pat.startsWith('group:')) { target = group ?? ''; glob = pat.slice('group:'.length) }
    else if (pat.startsWith('host:')) { target = host ?? ''; glob = pat.slice('host:'.length) }
    else { target = name ?? ''; glob = pat }
    if (globToRe(glob).test(target)) return true
  }
  return false
}

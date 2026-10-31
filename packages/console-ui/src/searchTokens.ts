/**
 * searchTokens.ts — the `host:` / `group:` search tokens shared by the
 * Marketplace and Forms search boxes.
 *
 * `host:web` keeps hosts whose name contains "web"; `group:prod` keeps hosts in
 * a sidebar group whose name contains "prod". Values are case-insensitive
 * substrings; several of one kind are OR'd, host and group together are AND'd.
 */

/** The search box split into free text and `host:` / `group:` tokens. */
export interface ParsedSearch {
  text: string
  hosts: string[]
  groups: string[]
}

export function parseSearch(q: string): ParsedSearch {
  const out: ParsedSearch = { text: '', hosts: [], groups: [] }
  const words: string[] = []
  for (const w of (q || '').trim().split(/\s+/)) {
    const m = /^(host|group):(.*)$/i.exec(w)
    if (!m) { if (w) words.push(w); continue }
    const v = m[2].trim().toLowerCase()
    if (!v) continue  // "host:" while still typing — no filter yet
    if (m[1].toLowerCase() === 'host') out.hosts.push(v)
    else out.groups.push(v)
  }
  out.text = words.join(' ')
  return out
}

/** True when the search names any `host:` / `group:` token. */
export function hasTargetTokens(p: ParsedSearch): boolean {
  return p.hosts.length > 0 || p.groups.length > 0
}

/** Whether a host (with its sidebar group, if any) passes the tokens. */
export function hostPasses(p: ParsedSearch, host: string, group: string | undefined): boolean {
  const h = host.toLowerCase()
  if (p.hosts.length && !p.hosts.some(v => h.includes(v))) return false
  const g = (group || '').toLowerCase()
  if (p.groups.length && !(g && p.groups.some(v => g.includes(v)))) return false
  return true
}

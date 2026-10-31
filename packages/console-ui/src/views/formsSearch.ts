/**
 * formsSearch.ts — which runnable cards the Forms tab shows, kept pure so it is
 * testable without rendering FormsView.
 *
 * Two scopes:
 *   - `host`    — the runnables on the host selected in the sidebar (the old
 *                 behaviour), narrowed by the venv scope chips.
 *   - `profile` — every host in the sidebar, i.e. the active host profile's
 *                 working set. A runnable found on several hosts is one card
 *                 that lists its hosts; picking one opens the form for it.
 *
 * The search box takes free text (every word must match the name, description,
 * venv, host or a subcommand) plus the Marketplace's `host:` / `group:` tokens.
 * A token always searches the whole profile, since narrowing to hosts only
 * makes sense across hosts.
 */

import type { Host, Runnable } from '../bridge'
import { hasTargetTokens, hostPasses, parseSearch } from '../searchTokens'

export type FormsScope = 'host' | 'profile'
export const FORMS_SCOPE_KEY = 'runspec.forms.scope'

/** One Forms card: a runnable (by venv + name) and the hosts it was found on. */
export interface FormsEntry {
  key: string
  name: string
  group: string
  /** One record per host, best pick first (selected host, then connected, then by name). */
  records: Runnable[]
}

export interface FormsSelection {
  entries: FormsEntry[]
  /** The scope actually applied (a host:/group: token or no selected host widens to `profile`). */
  scope: FormsScope
  /** In `host` scope with search text: cards that match only on other hosts. */
  elsewhere: number
}

/** Every word of `text` appears somewhere in the runnable's searchable fields. */
export function runnableMatchesText(r: Runnable, text: string): boolean {
  const words = text.toLowerCase().split(/\s+/).filter(Boolean)
  if (!words.length) return true
  const hay = [
    r.name, r.description ?? '', r.host, r.group,
    ...Object.entries(r.commands ?? {}).flatMap(([name, cmd]) => [name, cmd.description ?? '']),
  ].join('\n').toLowerCase()
  return words.every(w => hay.includes(w))
}

const entryKey = (r: Runnable) => `${r.group}\u0000${r.name}`

/** Collapse per-host records into one entry per venv + name, keeping first-seen order. */
export function collapseByRunnable(records: Runnable[], hosts: Host[], selectedHost: string): FormsEntry[] {
  const connected = new Map(hosts.map(h => [h.name, h.connected]))
  const byKey = new Map<string, FormsEntry>()
  for (const r of records) {
    const key = entryKey(r)
    let e = byKey.get(key)
    if (!e) { e = { key, name: r.name, group: r.group, records: [] }; byKey.set(key, e) }
    if (!e.records.some(x => x.host === r.host)) e.records.push(r)
  }
  const rank = (r: Runnable) => (r.host === selectedHost ? 0 : (connected.get(r.host) ?? true) ? 1 : 2)
  for (const e of byKey.values()) {
    e.records.sort((a, b) => rank(a) - rank(b) || a.host.localeCompare(b.host))
  }
  return [...byKey.values()]
}

export function selectForms(opts: {
  runnables: Runnable[]
  hosts: Host[]
  selectedHost: string
  scope: FormsScope
  activeScope: string[]
  search: string
}): FormsSelection {
  const { runnables, hosts, selectedHost, activeScope } = opts
  const parsed = parseSearch(opts.search)
  const scope: FormsScope =
    opts.scope === 'profile' || hasTargetTokens(parsed) || !selectedHost ? 'profile' : 'host'

  // The sidebar lists the active profile's hosts; runnables cached for a host
  // outside it don't belong in a profile-wide search. (No host list = no filter.)
  const hostGroup = new Map(hosts.map(h => [h.name, h.group]))
  const inProfile = hosts.length ? runnables.filter(r => hostGroup.has(r.host)) : runnables
  const matching = inProfile.filter(r =>
    hostPasses(parsed, r.host, hostGroup.get(r.host)) && runnableMatchesText(r, parsed.text))

  if (scope === 'profile') {
    return { entries: collapseByRunnable(matching, hosts, selectedHost), scope, elsewhere: 0 }
  }

  const onHost = matching.filter(r => r.host === selectedHost)
  const shown = activeScope.length ? onHost.filter(r => activeScope.includes(r.group)) : onHost
  let elsewhere = 0
  if (parsed.text) {
    const here = new Set(onHost.map(entryKey))
    elsewhere = new Set(matching.filter(r => !here.has(entryKey(r))).map(entryKey)).size
  }
  return { entries: collapseByRunnable(shown, hosts, selectedHost), scope, elsewhere }
}

// ── persistence (best-effort; localStorage may be unavailable) ──────────────

export function loadFormsScope(storage: Pick<Storage, 'getItem'> | null = safeStorage()): FormsScope {
  try {
    return storage?.getItem(FORMS_SCOPE_KEY) === 'profile' ? 'profile' : 'host'
  } catch {
    return 'host'
  }
}

export function saveFormsScope(scope: FormsScope, storage: Pick<Storage, 'setItem'> | null = safeStorage()): void {
  try {
    storage?.setItem(FORMS_SCOPE_KEY, scope)
  } catch { /* best-effort */ }
}

function safeStorage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null
  } catch {
    return null
  }
}

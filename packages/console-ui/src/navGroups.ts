/**
 * Top tab bar layout: the visible tabs in three groups (day-to-day work,
 * automation, knowledge) with a thin rule between groups, and the occasional
 * tabs folded into a More menu so the bar fits on one row at the default
 * window width.
 *
 * Pure so the grouping is unit-tested; App.tsx renders it. Ctrl+1…9 follows
 * the on-screen order (groups first, then the More items), so the number a
 * tab's tooltip shows is the key that opens it.
 */

export const NAV_GROUPS: readonly (readonly string[])[] = [
  ['console', 'forms', 'history', 'logs', 'files'],
  ['schedules', 'triggers'],
  ['knowledge', 'marketplace'],
]

/** Tabs that live in the More menu. */
export const NAV_OVERFLOW: readonly string[] = ['rota', 'specs', 'improve', 'evals', 'dev']

export interface NavLayout<T> {
  /** Non-empty groups, in NAV_GROUPS order. */
  groups: T[][]
  /** The More menu, in NAV_OVERFLOW order, then any tab no list names. */
  overflow: T[]
  /** groups flattened, then overflow: the Ctrl+1…9 order. */
  ordered: T[]
}

export function layoutNavTabs<T extends { key: string }>(tabs: T[]): NavLayout<T> {
  const byKey = new Map(tabs.map(t => [t.key, t]))
  const placed = new Set<string>()
  const pick = (keys: readonly string[]) => keys.flatMap(k => {
    const t = byKey.get(k)
    if (!t) return []
    placed.add(k)
    return [t]
  })
  const groups = NAV_GROUPS.map(pick).filter(g => g.length > 0)
  const overflow = [...pick(NAV_OVERFLOW), ...tabs.filter(t => !placed.has(t.key))]
  return { groups, overflow, ordered: [...groups.flat(), ...overflow] }
}

/** Does `query` match this host? Case-insensitive on its name or group. */
export function hostMatches(h: { name: string; group?: string }, query: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return h.name.toLowerCase().includes(q) || (h.group ?? '').toLowerCase().includes(q)
}

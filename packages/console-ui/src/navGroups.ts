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
  ['console', 'forms', 'history', 'files'],
  ['automation'],
  ['knowledge', 'marketplace'],
]

/** Tabs that live in the More menu. */
export const NAV_OVERFLOW: readonly string[] = ['quality', 'dev']

/**
 * Tabs that hold several views behind a section switch. Each view keeps its
 * own key (so code that opens "triggers" still works); the tab bar shows the
 * parent, and the parent opens the section used last.
 */
export const NAV_SECTIONS: Readonly<Record<string, readonly { key: string; label: string }[]>> = {
  forms: [{ key: 'forms', label: 'Run' }, { key: 'specs', label: 'Specs' }],
  history: [{ key: 'history', label: 'Runs' }, { key: 'analytics', label: 'Charts' }, { key: 'logs', label: 'Log files' }],
  automation: [{ key: 'schedules', label: 'Schedules' }, { key: 'triggers', label: 'Triggers' }, { key: 'webhooks', label: 'Webhooks' }, { key: 'rota', label: 'Working hours' }],
  quality: [{ key: 'improve', label: 'Improve' }, { key: 'evals', label: 'Evals' }],
}

/** The tab a view belongs to: its parent, or itself when it has none. */
export function parentOf(view: string): string {
  for (const [parent, sections] of Object.entries(NAV_SECTIONS)) {
    if (sections.some(s => s.key === view)) return parent
  }
  return view
}

/** The tabs to show for the visible views, in order of first appearance: a
 *  parent stands for its visible sections; a view with no parent stands for
 *  itself. A parent whose sections are all hidden (AI off) is dropped. */
export function groupViews(visible: readonly string[]): { key: string; sections: { key: string; label: string }[] }[] {
  const seen = new Map<string, { key: string; sections: { key: string; label: string }[] }>()
  for (const v of visible) {
    const p = parentOf(v)
    if (!seen.has(p)) seen.set(p, { key: p, sections: [] })
    const sec = NAV_SECTIONS[p]?.find(s => s.key === v)
    if (sec) seen.get(p)!.sections.push(sec)
  }
  for (const g of seen.values()) {
    const order = NAV_SECTIONS[g.key]
    if (order) g.sections.sort((a, b) => order.findIndex(s => s.key === a.key) - order.findIndex(s => s.key === b.key))
  }
  return [...seen.values()]
}

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

/**
 * Fit the tab bar to the width it has: tabs from the end of the last group move
 * to the front of More until the rest fit, so More and Search commands never
 * leave the screen. `ordered` (the Ctrl+1…9 order) is unchanged.
 *
 * `widths` are the measured tab widths by key; `sep` is the rule between
 * groups; `more` is the More button's width (reserved whenever More shows).
 */
export function fitNavLayout<T extends { key: string }>(
  layout: NavLayout<T>, widths: Record<string, number>, available: number, more: number, sep: number,
): NavLayout<T> {
  const flat = layout.groups.flatMap((g, gi) => g.map((t, i) => ({ t, gi, cost: (widths[t.key] ?? 0) + (gi > 0 && i === 0 ? sep : 0) })))
  const total = flat.reduce((s, x) => s + x.cost, 0)
  if (total + (layout.overflow.length > 0 ? more : 0) <= available) return layout
  let used = more
  let keep = 0
  while (keep < flat.length && used + flat[keep].cost <= available) { used += flat[keep].cost; keep++ }
  const groups: T[][] = []
  for (const { t, gi } of flat.slice(0, keep)) (groups[gi] ??= []).push(t)
  return {
    groups: groups.filter(Boolean),
    overflow: [...flat.slice(keep).map(x => x.t), ...layout.overflow],
    ordered: layout.ordered,
  }
}

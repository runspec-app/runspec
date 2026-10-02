/**
 * formsSections.ts — which Forms-tab sections (Saved runs, Runbooks, and each
 * runnable group) are collapsed, kept pure so it's testable without rendering
 * FormsView.
 *
 * Saved runs and Runbooks start collapsed; runnable groups start open. A click
 * on a section header records a per-section override; "Collapse all" /
 * "Expand all" set a bulk mode and clear the overrides, so a section that
 * appears later follows the operator's last bulk choice.
 */

export const SAVED_RUNS_SECTION = 'saved-runs'
export const RUNBOOKS_SECTION = 'runbooks'
export const FORMS_SECTIONS_KEY = 'runspec.forms.sections'

export type SectionsMode = 'default' | 'expanded' | 'collapsed'

export interface FormsSectionsState {
  mode: SectionsMode
  overrides: Record<string, boolean>
}

const DEFAULT_COLLAPSED = new Set([SAVED_RUNS_SECTION, RUNBOOKS_SECTION])

/** Key for a runnable group's section (prefixed so a group can't collide with the fixed lanes). */
export function groupSection(group: string): string {
  return `group:${group}`
}

export function isSectionCollapsed(state: FormsSectionsState, key: string): boolean {
  const o = state.overrides[key]
  if (o !== undefined) return o
  if (state.mode === 'default') return DEFAULT_COLLAPSED.has(key)
  return state.mode === 'collapsed'
}

export function toggleSection(state: FormsSectionsState, key: string): FormsSectionsState {
  return { mode: state.mode, overrides: { ...state.overrides, [key]: !isSectionCollapsed(state, key) } }
}

export function setAllSections(mode: 'expanded' | 'collapsed'): FormsSectionsState {
  return { mode, overrides: {} }
}

/** True when at least one of `keys` is open — the bulk toggle then offers "Collapse all". */
export function anySectionExpanded(state: FormsSectionsState, keys: string[]): boolean {
  return keys.some(k => !isSectionCollapsed(state, k))
}

// ── persistence (best-effort; localStorage may be unavailable) ──────────────

export function loadFormsSections(storage: Pick<Storage, 'getItem'> | null = safeStorage()): FormsSectionsState {
  try {
    const raw = JSON.parse(storage?.getItem(FORMS_SECTIONS_KEY) || '{}')
    const mode: SectionsMode = raw?.mode === 'expanded' || raw?.mode === 'collapsed' ? raw.mode : 'default'
    const overrides: Record<string, boolean> = {}
    if (raw?.overrides && typeof raw.overrides === 'object') {
      for (const [k, v] of Object.entries(raw.overrides)) if (typeof v === 'boolean') overrides[k] = v
    }
    return { mode, overrides }
  } catch {
    return { mode: 'default', overrides: {} }
  }
}

export function saveFormsSections(state: FormsSectionsState, storage: Pick<Storage, 'setItem'> | null = safeStorage()): void {
  try {
    storage?.setItem(FORMS_SECTIONS_KEY, JSON.stringify(state))
  } catch { /* best-effort */ }
}

function safeStorage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null
  } catch {
    return null
  }
}

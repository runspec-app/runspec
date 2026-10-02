import { describe, it, expect } from 'vitest'
import {
  RUNBOOKS_SECTION, SAVED_RUNS_SECTION, FORMS_SECTIONS_KEY,
  anySectionExpanded, groupSection, isSectionCollapsed, loadFormsSections,
  saveFormsSections, setAllSections, toggleSection, type FormsSectionsState,
} from './formsSections'

const fresh: FormsSectionsState = { mode: 'default', overrides: {} }

describe('formsSections', () => {
  it('defaults: saved runs + runbooks collapsed, groups open', () => {
    expect(isSectionCollapsed(fresh, SAVED_RUNS_SECTION)).toBe(true)
    expect(isSectionCollapsed(fresh, RUNBOOKS_SECTION)).toBe(true)
    expect(isSectionCollapsed(fresh, groupSection('ops'))).toBe(false)
  })

  it('toggle flips one section; bulk mode clears overrides', () => {
    const t = toggleSection(fresh, SAVED_RUNS_SECTION)
    expect(isSectionCollapsed(t, SAVED_RUNS_SECTION)).toBe(false)
    const all = setAllSections('collapsed')
    expect(isSectionCollapsed(all, groupSection('new-group'))).toBe(true)
    expect(anySectionExpanded(all, [SAVED_RUNS_SECTION, groupSection('x')])).toBe(false)
  })

  it('round-trips through storage and tolerates junk', () => {
    const store = new Map<string, string>()
    const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) }
    saveFormsSections(setAllSections('expanded'), storage)
    expect(loadFormsSections(storage).mode).toBe('expanded')
    store.set(FORMS_SECTIONS_KEY, 'not json')
    expect(loadFormsSections(storage)).toEqual(fresh)
  })
})

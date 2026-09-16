import { describe, it, expect } from 'vitest'
import {
  anyExpanded, isGroupCollapsed, loadSidebarGroups, saveSidebarGroups,
  setAllGroups, toggleGroupState, COLLAPSED_GROUPS_KEY, GROUPS_MODE_KEY,
} from './sidebarGroups'

class MemStorage {
  data = new Map<string, string>()
  getItem(k: string) { return this.data.get(k) ?? null }
  setItem(k: string, v: string) { this.data.set(k, v) }
}

describe('sidebar group collapse state', () => {
  it('defaults to expanded; a per-group toggle overrides', () => {
    let s = setAllGroups('expanded')
    expect(isGroupCollapsed(s, 'Prod')).toBe(false)
    s = toggleGroupState(s, 'Prod')
    expect(isGroupCollapsed(s, 'Prod')).toBe(true)
    expect(isGroupCollapsed(s, 'Staging')).toBe(false)
  })

  it('collapse all clears overrides and makes unseen groups collapsed too', () => {
    let s = toggleGroupState(setAllGroups('expanded'), 'Prod')
    s = setAllGroups('collapsed')
    expect(s.overrides).toEqual({})
    expect(isGroupCollapsed(s, 'Prod')).toBe(true)
    expect(isGroupCollapsed(s, 'brand-new-group')).toBe(true)
    // A single toggle re-opens just that group.
    s = toggleGroupState(s, 'Prod')
    expect(isGroupCollapsed(s, 'Prod')).toBe(false)
    expect(isGroupCollapsed(s, 'Staging')).toBe(true)
    expect(anyExpanded(s, ['Prod', 'Staging'])).toBe(true)
    expect(anyExpanded(setAllGroups('collapsed'), ['Prod'])).toBe(false)
  })

  it('round-trips through storage and survives garbage', () => {
    const st = new MemStorage()
    saveSidebarGroups(toggleGroupState(setAllGroups('collapsed'), 'Prod'), st)
    expect(st.getItem(GROUPS_MODE_KEY)).toBe('collapsed')
    const back = loadSidebarGroups(st)
    expect(back.mode).toBe('collapsed')
    expect(isGroupCollapsed(back, 'Prod')).toBe(false)
    expect(isGroupCollapsed(back, 'Other')).toBe(true)

    st.setItem(COLLAPSED_GROUPS_KEY, '{not json')
    st.setItem(GROUPS_MODE_KEY, 'sideways')
    const bad = loadSidebarGroups(st)
    expect(bad).toEqual({ mode: 'expanded', overrides: {} })
    expect(loadSidebarGroups(null)).toEqual({ mode: 'expanded', overrides: {} })
  })
})

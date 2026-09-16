/**
 * sidebarGroups.ts — the sidebar's collapsed-group state, kept pure so it's
 * testable without rendering App.
 *
 * Two persisted pieces: a per-group override map (group → collapsed?) and a
 * default mode that a group *absent* from the map follows. "Collapse all" /
 * "Expand all" set the mode and clear the map, so a group that appears later
 * (a new fleet group after a Config Sync pull) follows the operator's last bulk
 * choice instead of silently springing open.
 */

export type GroupsMode = 'expanded' | 'collapsed'

export const COLLAPSED_GROUPS_KEY = 'runspec.sidebar.collapsedGroups'
export const GROUPS_MODE_KEY = 'runspec.sidebar.groupsMode'

export interface SidebarGroupsState {
  mode: GroupsMode
  overrides: Record<string, boolean>
}

export function isGroupCollapsed(state: SidebarGroupsState, group: string): boolean {
  const o = state.overrides[group]
  if (o !== undefined) return o
  return state.mode === 'collapsed'
}

export function toggleGroupState(state: SidebarGroupsState, group: string): SidebarGroupsState {
  return {
    mode: state.mode,
    overrides: { ...state.overrides, [group]: !isGroupCollapsed(state, group) },
  }
}

export function setAllGroups(mode: GroupsMode): SidebarGroupsState {
  return { mode, overrides: {} }
}

/** True when at least one of `groups` is currently expanded. */
export function anyExpanded(state: SidebarGroupsState, groups: string[]): boolean {
  return groups.some(g => !isGroupCollapsed(state, g))
}

// ── persistence (best-effort; localStorage may be unavailable) ──────────────

export function loadSidebarGroups(storage: Pick<Storage, 'getItem'> | null = safeStorage()): SidebarGroupsState {
  let overrides: Record<string, boolean> = {}
  let mode: GroupsMode = 'expanded'
  try {
    overrides = JSON.parse(storage?.getItem(COLLAPSED_GROUPS_KEY) || '{}')
    if (!overrides || typeof overrides !== 'object') overrides = {}
  } catch {
    overrides = {}
  }
  try {
    const m = storage?.getItem(GROUPS_MODE_KEY)
    if (m === 'collapsed' || m === 'expanded') mode = m
  } catch { /* keep default */ }
  return { mode, overrides }
}

export function saveSidebarGroups(state: SidebarGroupsState, storage: Pick<Storage, 'setItem'> | null = safeStorage()): void {
  try {
    storage?.setItem(COLLAPSED_GROUPS_KEY, JSON.stringify(state.overrides))
    storage?.setItem(GROUPS_MODE_KEY, state.mode)
  } catch { /* ignore */ }
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null
  } catch {
    return null
  }
}

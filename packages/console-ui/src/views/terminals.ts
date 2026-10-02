/**
 * Shared helpers for opening SSH terminals on several hosts at once — the
 * TerminalsModal and the sidebar group header's one-click "Open SSH
 * terminals" both use these. Kept out of the modal so App can call
 * `openTerminals` without pulling the lazily-loaded dialog into the main chunk.
 */
import { message } from 'antd'
import { bridge, type JumpHost, type LaunchTerminalsResult } from '../bridge'

const LAYOUT_KEY = 'rs.terminals.layout'
/** Pre-layout on/off preference — read once so an operator who turned
 *  tiling off keeps it off. */
const LEGACY_TILE_KEY = 'rs.terminals.tile'
/** Past this many windows a grid is too small to read; the backend cascades. */
export const GRID_MAX = 16

/** How the new terminal windows are arranged on screen. `none` leaves them
 *  wherever Windows puts them. */
export type TerminalLayout = 'grid' | 'compact' | 'columns' | 'rows' | 'cascade' | 'none'

export const LAYOUT_OPTIONS: { value: TerminalLayout; label: string }[] = [
  { value: 'grid', label: 'Grid' },
  { value: 'compact', label: 'Compact grid' },
  { value: 'columns', label: 'Side by side' },
  { value: 'rows', label: 'Stacked' },
  { value: 'cascade', label: 'Cascade' },
  { value: 'none', label: "Don't arrange" },
]

const LAYOUTS = new Set<string>(LAYOUT_OPTIONS.map(o => o.value))

export function readLayoutPref(): TerminalLayout {
  try {
    const v = localStorage.getItem(LAYOUT_KEY)
    if (v && LAYOUTS.has(v)) return v as TerminalLayout
    return localStorage.getItem(LEGACY_TILE_KEY) === '0' ? 'none' : 'grid'
  } catch {
    return 'grid'
  }
}

export function writeLayoutPref(layout: TerminalLayout): void {
  try {
    localStorage.setItem(LAYOUT_KEY, layout)
  } catch {
    /* private window / blocked storage — the default (grid) is fine */
  }
}

/** The bridge call for a layout: `none` turns tiling off. */
export function launchWithLayout(hosts: string[], layout: TerminalLayout): Promise<LaunchTerminalsResult> {
  return layout === 'none'
    ? bridge.launch_terminals(hosts, false)
    : bridge.launch_terminals(hosts, true, layout)
}

/** Host names in a sidebar group (`''` = ungrouped), in config order. */
export function hostsInGroup(hosts: JumpHost[], group: string): string[] {
  return hosts.filter(h => (h.group ?? '') === group).map(h => h.name)
}

/** Host names tagged with a profile, in config order. */
export function hostsInProfile(hosts: JumpHost[], profile: string): string[] {
  return hosts.filter(h => (h.profiles ?? []).includes(profile)).map(h => h.name)
}

/** `prev` plus any of `add` not already in it — keeps pick order. */
export function addToSelection(prev: string[], add: string[]): string[] {
  const have = new Set(prev)
  return [...prev, ...add.filter(n => !have.has(n))]
}

/** Open terminals on `hosts` with the remembered layout and report
 *  the outcome as a toast — the one-click path from a group header. */
export async function openTerminals(hosts: string[]): Promise<LaunchTerminalsResult | null> {
  if (!hosts.length) return null
  try {
    const res = await launchWithLayout(hosts, readLayoutPref())
    if (res.errors.length) {
      message.warning(
        `Opened ${res.opened.length} of ${hosts.length}. ` +
        res.errors.map(e => `${e.host}: ${e.error}`).join('; '),
      )
    } else {
      message.success(`Opened ${res.opened.length} terminal${res.opened.length === 1 ? '' : 's'}`)
    }
    return res
  } catch (err) {
    message.error((err as Error)?.message || 'could not open terminals')
    return null
  }
}


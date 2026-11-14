/**
 * OtherHostsSection — the sidebar's "Other hosts" list: configured hosts that
 * are outside the active host profile.
 *
 * The profile's own hosts stay above, exactly as before (probed, connected,
 * runnables loaded). These rows are never probed, so they show a hollow ring
 * instead of a connected dot, and they only offer actions that make their own
 * connection (PuTTY, copying the SSH key) or change the profile. Clicking a row
 * opens its menu rather than selecting it, so Forms never shows an empty page
 * for a host whose runnables aren't loaded.
 */
import { useState } from 'react'
import { Dropdown, Tooltip, type MenuProps } from 'antd'
import { CodeOutlined, DownOutlined, KeyOutlined, PlusOutlined, RightOutlined, SwapOutlined } from '@ant-design/icons'
import type { OtherHost } from '../bridge'
import { MONO_FONT, textFaint, textMuted, textSecondary } from '../ThemeContext'
import { pressable } from './kit'

export const OTHER_HOSTS_OPEN_KEY = 'runspec.sidebar.otherHostsOpen'

/** Prefix for the section's group keys in the shared sidebar collapse state, so
 *  a group's state here is separate from the same group in the profile list. */
export const OTHER_GROUP_PREFIX = 'other:'

/** Hosts bucketed by group in file order; group keys sorted, ungrouped first
 *  (the same order as the profile list above). */
export function groupOtherHosts(hosts: OtherHost[]): { keys: string[]; map: Map<string, OtherHost[]> } {
  const map = new Map<string, OtherHost[]>()
  for (const h of hosts) {
    const g = h.group ?? ''
    if (!map.has(g)) map.set(g, [])
    map.get(g)!.push(h)
  }
  const keys = [...map.keys()].sort((a, b) => {
    if (!a && b) return -1
    if (a && !b) return 1
    return a.localeCompare(b)
  })
  return { keys, map }
}

export interface SyncSource {
  url?: string
  files?: string[]
}

/** True when Config Sync pulls runspec_hosts.toml (a source is set and the
 *  file is in its list; an empty list means the default set, which has it). */
export function syncsHostsFile(sync: SyncSource | null | undefined): boolean {
  if (!sync || !String(sync.url || '').trim()) return false
  const files = sync.files ?? []
  return files.length === 0 || files.includes('runspec_hosts.toml')
}

/** Hover text for "Add to / Remove from <profile>": where the change is saved,
 *  and that a Config Sync pull can put it back. */
export function profileEditNote(sync: SyncSource | null | undefined): string {
  if (!syncsHostsFile(sync)) {
    return 'Saved in runspec_hosts.toml on this machine.'
  }
  return (
    `Saved in runspec_hosts.toml on this machine. Config Sync pulls this file from ${String(sync!.url).trim()}: ` +
    'if the host comes from there, the next pull puts its profiles back to what the repo has. ' +
    'Change the repo too to keep it.'
  )
}

function readOpen(): boolean {
  try {
    return localStorage.getItem(OTHER_HOSTS_OPEN_KEY) !== 'false'
  } catch {
    return true
  }
}

function writeOpen(open: boolean): void {
  try {
    localStorage.setItem(OTHER_HOSTS_OPEN_KEY, open ? 'true' : 'false')
  } catch { /* ignore */ }
}

/** The hollow ring: "not watched" (outside the profile, never probed). */
export function HollowRing({ color }: { color: string }) {
  return (
    <span
      aria-hidden
      style={{
        display: 'inline-block', width: 7, height: 7, borderRadius: '50%',
        border: `1.5px solid ${color}`, boxSizing: 'border-box', flexShrink: 0,
      }}
    />
  )
}

export interface OtherHostsSectionProps {
  hosts: OtherHost[]
  activeProfile: string
  isDark: boolean
  /** Hover text on "Add to <profile>" (see profileEditNote). */
  editNote: string
  isGroupCollapsed: (key: string) => boolean
  onToggleGroup: (key: string) => void
  onOpenTerminal: (host: string) => void
  onOpenTerminals: (hosts: string[]) => void
  onChooseTerminals: (hosts: string[]) => void
  onCopyKey: (host: string) => void
  onAddToProfile: (host: string) => void
  onSwitchProfile: (profile: string) => void
}

export function OtherHostsSection(props: OtherHostsSectionProps) {
  const {
    hosts, activeProfile, isDark, editNote, isGroupCollapsed, onToggleGroup,
    onOpenTerminal, onOpenTerminals, onChooseTerminals, onCopyKey, onAddToProfile, onSwitchProfile,
  } = props
  const [open, setOpen] = useState(readOpen)
  if (!activeProfile || hosts.length === 0) return null

  const muted = textMuted(isDark)
  const faint = textFaint(isDark)
  const ring = isDark ? '#6a6a6a' : '#a0a0a0'
  const { keys, map } = groupOtherHosts(hosts)
  const toggleOpen = () => { setOpen(o => { writeOpen(!o); return !o }) }

  const hostMenu = (h: OtherHost): MenuProps['items'] => {
    const switchTo = h.profiles.filter(p => p.toLowerCase() !== activeProfile.toLowerCase())
    return [
      { key: 'open_terminal', icon: <CodeOutlined />, label: 'Open SSH terminal here', onClick: () => onOpenTerminal(h.name) },
      { key: 'open_terminals', icon: <CodeOutlined />, label: 'Open SSH terminals on several hosts…', onClick: () => onChooseTerminals([h.name]) },
      { key: 'copy_ssh_id', icon: <KeyOutlined />, label: 'Copy SSH public key…', onClick: () => onCopyKey(h.name) },
      { type: 'divider' },
      {
        key: 'add_to_profile',
        icon: <PlusOutlined />,
        label: (
          <Tooltip title={editNote} placement="right">
            <span>Add to {activeProfile}</span>
          </Tooltip>
        ),
        onClick: () => onAddToProfile(h.name),
      },
      ...switchTo.map(p => ({
        key: `switch:${p}`,
        icon: <SwapOutlined />,
        label: `Switch to profile ${p}`,
        onClick: () => onSwitchProfile(p),
      })),
      { type: 'divider' as const },
      {
        key: 'note',
        disabled: true,
        label: (
          <span style={{ display: 'block', maxWidth: 240, whiteSpace: 'normal', fontSize: 11, lineHeight: 1.4, color: faint }}>
            Outside {activeProfile}: no standing connection, runnables not loaded
          </span>
        ),
      },
    ]
  }

  return (
    <div data-testid="other-hosts">
      <div
        className="rs-group-header"
        role="button"
        aria-expanded={open}
        onClick={toggleOpen}
        style={{
          display: 'flex', alignItems: 'center', gap: 6,
          margin: '10px 10px 0', padding: '8px 4px 0',
          borderTop: `1px dashed ${isDark ? '#3a3a3a' : '#d0d0d0'}`,
          fontSize: 11, color: textSecondary(isDark),
          textTransform: 'uppercase', letterSpacing: '0.08em',
          userSelect: 'none', cursor: 'pointer',
        }}
      >
        {open ? <DownOutlined style={{ fontSize: 8 }} /> : <RightOutlined style={{ fontSize: 8 }} />}
        <span style={{ flex: 1 }}>Other hosts</span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 4, letterSpacing: 0, color: muted }}>
          <HollowRing color={ring} /> {hosts.length}
        </span>
      </div>
      {open && (
        <>
          <div style={{ fontSize: 11, color: faint, padding: '2px 14px 0', lineHeight: 1.4 }}>
            Not in {activeProfile}: terminal only, runnables not loaded
          </div>
          {keys.map(g => {
            const members = map.get(g)!
            const key = OTHER_GROUP_PREFIX + g
            const collapsed = isGroupCollapsed(key)
            const names = members.map(m => m.name)
            return (
              <div key={key}>
                <Dropdown
                  trigger={['contextMenu']}
                  menu={{
                    items: [
                      {
                        key: 'open_group_terminals',
                        icon: <CodeOutlined />,
                        label: `Open SSH terminals (${names.length})`,
                        onClick: () => onOpenTerminals(names),
                      },
                      {
                        key: 'choose_group_terminals',
                        icon: <CodeOutlined />,
                        label: 'Choose hosts…',
                        onClick: () => onChooseTerminals(names),
                      },
                    ],
                  }}
                >
                  <div
                    className="rs-group-header"
                    {...pressable(() => onToggleGroup(key), { expanded: !collapsed })}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 6,
                      padding: '10px 14px 3px',
                      fontSize: 11, color: textSecondary(isDark),
                      textTransform: 'uppercase', letterSpacing: '0.08em',
                      userSelect: 'none', cursor: 'pointer',
                    }}
                  >
                    {collapsed ? <RightOutlined style={{ fontSize: 8 }} /> : <DownOutlined style={{ fontSize: 8 }} />}
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {g || 'Hosts'}
                    </span>
                    {collapsed && (
                      <span style={{ display: 'flex', alignItems: 'center', gap: 4, letterSpacing: 0, color: muted }}>
                        <HollowRing color={ring} /> {members.length}
                      </span>
                    )}
                  </div>
                </Dropdown>
                {!collapsed && members.map(h => (
                  <Dropdown key={h.name} trigger={['click', 'contextMenu']} menu={{ items: hostMenu(h) }}>
                    <div
                      className="rs-host-row"
                      data-testid={`other-host-${h.name}`}
                      title={`${h.name} is outside ${activeProfile}`}
                      // Opens the host's menu (the Dropdown listens for click), so
                      // Enter/Space click it too.
                      role="button" tabIndex={0} aria-haspopup="menu"
                      onKeyDown={e => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); (e.currentTarget as HTMLElement).click() } }}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 8,
                        padding: '8px 12px 8px 14px', cursor: 'pointer',
                        borderLeft: '2px solid transparent',
                      }}
                    >
                      <HollowRing color={ring} />
                      <span style={{
                        fontSize: 14, color: muted, flex: 1, fontFamily: MONO_FONT,
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      }}>{h.name}</span>
                    </div>
                  </Dropdown>
                ))}
              </div>
            )
          })}
        </>
      )}
    </div>
  )
}

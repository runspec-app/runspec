/**
 * The sidebar's Other hosts section: hosts outside the active profile, grouped,
 * with a menu on click and right-click (terminal, copy key, add to profile,
 * switch profile) and nothing at all under All hosts.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import {
  OtherHostsSection, groupOtherHosts, profileEditNote, syncsHostsFile, OTHER_HOSTS_OPEN_KEY,
  type OtherHostsSectionProps,
} from './OtherHostsSection'
import type { OtherHost } from '../bridge'

const HOSTS: OtherHost[] = [
  { name: 'bastion', group: 'devops', profiles: ['devops'] },
  { name: 'db-01', group: 'prod', profiles: [] },
  { name: 'loose', group: null, profiles: [] },
  { name: 'ci-01', group: 'devops', profiles: ['devops', 'ci'] },
]

function renderSection(over: Partial<OtherHostsSectionProps> = {}) {
  const props: OtherHostsSectionProps = {
    hosts: HOSTS,
    activeProfile: 'app-support',
    isDark: true,
    editNote: 'note',
    isGroupCollapsed: () => false,
    onToggleGroup: vi.fn(),
    onOpenTerminal: vi.fn(),
    onOpenTerminals: vi.fn(),
    onChooseTerminals: vi.fn(),
    onCopyKey: vi.fn(),
    onAddToProfile: vi.fn(),
    onSwitchProfile: vi.fn(),
    ...over,
  }
  render(<OtherHostsSection {...props} />)
  return props
}

beforeEach(() => { localStorage.clear() })

describe('groupOtherHosts', () => {
  it('groups by group, ungrouped first, then alphabetical; members keep file order', () => {
    const { keys, map } = groupOtherHosts(HOSTS)
    expect(keys).toEqual(['', 'devops', 'prod'])
    expect(map.get('devops')!.map(h => h.name)).toEqual(['bastion', 'ci-01'])
  })
})

describe('profileEditNote', () => {
  it('says the change is local when Config Sync is not set up', () => {
    expect(syncsHostsFile(null)).toBe(false)
    expect(syncsHostsFile({ url: '', files: [] })).toBe(false)
    expect(profileEditNote(null)).toBe('Saved in runspec_hosts.toml on this machine.')
  })

  it('warns that a pull can reset it when Config Sync pulls the hosts file', () => {
    const sync = { url: 'https://git.example/ops/config.git', files: ['config.toml', 'runspec_hosts.toml'] }
    expect(syncsHostsFile(sync)).toBe(true)
    expect(profileEditNote(sync)).toContain('https://git.example/ops/config.git')
    expect(profileEditNote(sync)).toContain('next pull')
  })

  it('treats a source that leaves out the hosts file as local', () => {
    expect(syncsHostsFile({ url: 'https://git.example/x.git', files: ['config.toml'] })).toBe(false)
  })
})

describe('OtherHostsSection', () => {
  it('renders nothing under All hosts or with no other hosts', () => {
    const { container } = render(
      <OtherHostsSection {...{
        hosts: HOSTS, activeProfile: '', isDark: true, editNote: '',
        isGroupCollapsed: () => false, onToggleGroup: vi.fn(), onOpenTerminal: vi.fn(),
        onOpenTerminals: vi.fn(), onChooseTerminals: vi.fn(), onCopyKey: vi.fn(),
        onAddToProfile: vi.fn(), onSwitchProfile: vi.fn(),
      }} />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('lists every other host with the count and explains what it means', () => {
    renderSection()
    expect(screen.getByText('Other hosts')).toBeTruthy()
    expect(screen.getByText(/Not in app-support/)).toBeTruthy()
    for (const h of HOSTS) expect(screen.getByTestId(`other-host-${h.name}`)).toBeTruthy()
  })

  it('collapses and remembers it', () => {
    renderSection()
    fireEvent.click(screen.getByText('Other hosts'))
    expect(screen.queryByTestId('other-host-bastion')).toBeNull()
    expect(localStorage.getItem(OTHER_HOSTS_OPEN_KEY)).toBe('false')
  })

  it('a click opens the host menu; its actions call back with the host', async () => {
    const props = renderSection()
    fireEvent.click(screen.getByTestId('other-host-ci-01'))
    await waitFor(() => expect(screen.getByText('Open SSH terminal here')).toBeTruthy())
    expect(screen.getByText('Switch to profile devops')).toBeTruthy()
    expect(screen.getByText('Switch to profile ci')).toBeTruthy()

    fireEvent.click(screen.getByText('Add to app-support'))
    expect(props.onAddToProfile).toHaveBeenCalledWith('ci-01')
  })

  it('opens a terminal and switches profile from the menu', async () => {
    const props = renderSection()
    fireEvent.contextMenu(screen.getByTestId('other-host-bastion'))
    await waitFor(() => expect(screen.getByText('Open SSH terminal here')).toBeTruthy())
    fireEvent.click(screen.getByText('Open SSH terminal here'))
    expect(props.onOpenTerminal).toHaveBeenCalledWith('bastion')

    fireEvent.contextMenu(screen.getByTestId('other-host-bastion'))
    await waitFor(() => expect(screen.getByText('Switch to profile devops')).toBeTruthy())
    fireEvent.click(screen.getByText('Switch to profile devops'))
    expect(props.onSwitchProfile).toHaveBeenCalledWith('devops')
  })

  it('a group header right-click opens terminals on that group', async () => {
    const props = renderSection()
    fireEvent.contextMenu(screen.getByText('devops'))
    await waitFor(() => expect(screen.getByText('Open SSH terminals (2)')).toBeTruthy())
    fireEvent.click(screen.getByText('Open SSH terminals (2)'))
    expect(props.onOpenTerminals).toHaveBeenCalledWith(['bastion', 'ci-01'])
  })
})

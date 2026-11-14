/**
 * Settings is a wide modal with its tabs along the top (it was a right-hand
 * drawer with tabs down the left that scrolled out of view). Every tab is in
 * the strip (none hidden behind an overflow menu), and the strip sits outside
 * the scrolling content holder so it stays visible.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { SettingsDrawer, SettingsTabBar, elsewhere, groupSettingsTabs, matchSettingsTab } from './SettingsDrawer'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, prop) => {
      if (typeof prop === 'symbol') return undefined
      if (prop === 'get_config') return async () => ({})
      if (prop === 'config_dir') return async () => 'C:/cfg'
      if (prop === 'get_hosts' || prop === 'get_jump_hosts' || prop === 'get_groups' || prop === 'get_profiles') return async () => []
      return vi.fn().mockResolvedValue(undefined)
    },
  }),
}))

describe('Settings modal', () => {
  it('renders as a dialog with every tab in a top strip outside the scroll area', async () => {
    render(<SettingsDrawer open onClose={() => {}} />)
    await screen.findByRole('dialog')
    expect(document.querySelector('.rs-settings-modal')).toBeTruthy()
    const strip = screen.getByRole('tablist', { name: 'Settings sections' })
    const labels = screen.getAllByRole('tab').map(t => t.textContent)
    expect(labels).toEqual(expect.arrayContaining(['SSH', 'Hosts', 'Telemetry', 'Voice']))
    // No antd overflow ("more") dropdown: the strip wraps instead.
    expect(document.querySelector('.ant-tabs-nav-operations')).toBeNull()
    // The strip is not inside the scrolling content holder.
    expect(strip.closest('.ant-tabs-content-holder')).toBeNull()
  })

  it('switches tab when a strip button is clicked', async () => {
    render(<SettingsDrawer open onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('tab', { name: 'Hosts' }))
    await waitFor(() => {
      const sel = screen.getAllByRole('tab').find(t => t.getAttribute('aria-selected') === 'true')
      expect(sel?.textContent).toBe('Hosts')
    })
  })
})

describe('Settings search', () => {
  it('finds a section by what it holds, and Enter opens the first match', () => {
    const onSelect = vi.fn()
    render(<SettingsTabBar activeKey="ssh" onSelect={onSelect} items={[
      { key: 'ssh', label: 'SSH' }, { key: 'telemetry', label: 'Telemetry' }, { key: 'credentials', label: 'Credentials' },
    ]} />)
    const box = screen.getByLabelText('Search settings sections')
    fireEvent.change(box, { target: { value: 'otel' } })
    expect(screen.getAllByRole('tab').map(t => t.textContent)).toEqual(['Telemetry'])
    fireEvent.keyDown(box, { key: 'Enter', code: 'Enter', keyCode: 13 })
    expect(onSelect).toHaveBeenCalledWith('telemetry')
    fireEvent.change(box, { target: { value: 'zzzz' } })
    expect(screen.getByText('No section matches.')).toBeTruthy()
  })

  it('points to features that live outside Settings', () => {
    expect(elsewhere('denylist')).toMatch(/hosts list/)
    expect(elsewhere('kill')).toMatch(/kill switch/)
    expect(elsewhere('zzzz')).toBeNull()
  })

  it('matches every word against the name and the keywords', () => {
    expect(matchSettingsTab({ key: 'credentials', label: 'Credentials' }, 'keychain token')).toBe(true)
    expect(matchSettingsTab({ key: 'credentials', label: 'Credentials' }, 'keychain proxy')).toBe(false)
    expect(matchSettingsTab({ key: 'ssh', label: 'SSH' }, '')).toBe(true)
  })

  it('pins Save to the bottom of the SSH section, not mid-column', async () => {
    render(<SettingsDrawer open onClose={() => {}} />)
    const bar = await screen.findByTestId('save-bar')
    expect(bar.style.position).toBe('sticky')
    expect(bar.closest('.rs-settings-cols')).toBeNull()
  })
})

describe('groupSettingsTabs', () => {
  it('buckets tabs into the five groups in order, dropping empty groups and keeping unknown tabs', () => {
    const t = (...keys: string[]) => keys.map(key => ({ key }))
    const groups = groupSettingsTabs(t('telemetry', 'ssh', 'credentials', 'jumpHosts', 'brandnew'))
    expect(groups.map(g => g.label)).toEqual(['Connections', 'Access & sync', 'Diagnostics'])
    expect(groups[0].items.map(i => i.key)).toEqual(['ssh', 'jumpHosts'])
    expect(groups[2].items.map(i => i.key)).toEqual(['telemetry', 'brandnew'])
  })
})

describe('Terminal section', () => {
  it('sits in Connections next to SSH', () => {
    const groups = groupSettingsTabs([{ key: 'ssh' }, { key: 'terminal' }, { key: 'jumpHosts' }])
    expect(groups[0]).toMatchObject({ label: 'Connections' })
    expect(groups[0].items.map(i => i.key)).toEqual(['ssh', 'terminal', 'jumpHosts'])
  })
})

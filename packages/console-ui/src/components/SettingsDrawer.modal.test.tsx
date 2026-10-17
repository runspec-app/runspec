/**
 * Settings is a wide modal with its tabs along the top (it was a right-hand
 * drawer with tabs down the left that scrolled out of view). Every tab is in
 * the strip (none hidden behind an overflow menu), and the strip sits outside
 * the scrolling content holder so it stays visible.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { SettingsDrawer } from './SettingsDrawer'

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
    expect(labels).toEqual(expect.arrayContaining(['SSH', 'Jump Hosts', 'Telemetry', 'Voice']))
    // No antd overflow ("more") dropdown: the strip wraps instead.
    expect(document.querySelector('.ant-tabs-nav-operations')).toBeNull()
    // The strip is not inside the scrolling content holder.
    expect(strip.closest('.ant-tabs-content-holder')).toBeNull()
  })

  it('switches tab when a strip button is clicked', async () => {
    render(<SettingsDrawer open onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('tab', { name: 'Jump Hosts' }))
    await waitFor(() => {
      const sel = screen.getAllByRole('tab').find(t => t.getAttribute('aria-selected') === 'true')
      expect(sel?.textContent).toBe('Jump Hosts')
    })
  })
})

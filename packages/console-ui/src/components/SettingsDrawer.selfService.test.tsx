/**
 * The Self-Service tab (helpdesk catalogue + rooms + the folded-in Teams manager)
 * is hidden pending internal application approval (SHOW_HELPDESK_ROOMS in
 * SettingsDrawer.tsx). This asserts neither Self-Service nor a standalone Teams
 * tab is offered in Settings. The tab component + its bridge calls are left
 * intact; flip SHOW_HELPDESK_ROOMS to re-expose (and restore the richer test).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

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

describe('Settings → Self-Service (hidden pending approval)', () => {
  it('offers neither a Self-Service nor a Teams tab', async () => {
    render(<SettingsDrawer open onClose={() => {}} />)
    const tabs = await screen.findAllByRole('tab')
    const labels = tabs.map(t => t.textContent)
    expect(labels).not.toContain('Self-Service')
    expect(labels).not.toContain('Teams')
    // The neighbouring tabs are unaffected.
    expect(labels).toContain('Repos')
    expect(labels).toContain('Credentials')
  })

  it('no longer duplicates editors that live in their own views', async () => {
    // Saved runs + Runbooks are managed from Forms, Filters from the Transfer
    // dialog, Marketplace settings from the Marketplace tab.
    render(<SettingsDrawer open onClose={() => {}} />)
    const labels = (await screen.findAllByRole('tab')).map(t => t.textContent)
    for (const gone of ['Saved runs', 'Runbooks', 'Filters', 'Marketplace']) {
      expect(labels).not.toContain(gone)
    }
  })
})

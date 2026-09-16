/**
 * The Teams manager lives on the Self-Service tab (next to the room selector) —
 * the standalone Teams tab was folded in. Asserts the section renders, lists the
 * teams pushed by the `runspec:teams` event, and adds through the bridge.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { SettingsDrawer } from './SettingsDrawer'

const { listTeams, addTeam, getSelfService, getRunnables, getRoomHosts } = vi.hoisted(() => ({
  listTeams: vi.fn(),
  addTeam: vi.fn(),
  getSelfService: vi.fn(),
  getRunnables: vi.fn(),
  getRoomHosts: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, prop) => {
      if (typeof prop === 'symbol') return undefined
      if (prop === 'list_teams') return listTeams
      if (prop === 'add_team') return addTeam
      if (prop === 'get_self_service') return getSelfService
      if (prop === 'get_runnables') return getRunnables
      if (prop === 'get_room_hosts') return getRoomHosts
      if (prop === 'get_active_room') return async () => 'ops-1'
      if (prop === 'get_config') return async () => ({})
      if (prop === 'config_dir') return async () => 'C:/cfg'
      if (prop === 'get_hosts' || prop === 'get_jump_hosts' || prop === 'get_groups' || prop === 'get_profiles') return async () => []
      return vi.fn().mockResolvedValue(undefined)
    },
  }),
}))

describe('Settings → Self-Service', () => {
  beforeEach(() => {
    listTeams.mockReset().mockResolvedValue({ ok: true })
    addTeam.mockReset().mockResolvedValue({ ok: true })
    getSelfService.mockReset().mockResolvedValue([{ name: 'reset-password' }])
    getRunnables.mockReset().mockResolvedValue([
      { name: 'reset-password', group: 'helpdesk', host: 'local', args: [], rawSpec: {} },
    ])
    getRoomHosts.mockReset().mockResolvedValue([{ host: 'ops-1', queue: 'it-support', room: 'ops' }])
  })

  it('has no Teams tab; the Self-Service tab hosts the Teams section', async () => {
    render(<SettingsDrawer open onClose={() => {}} />)
    const tabs = await screen.findAllByRole('tab')
    const labels = tabs.map(t => t.textContent)
    expect(labels).toContain('Self-Service')
    expect(labels).not.toContain('Teams')

    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByRole('tab', { name: 'Self-Service' })) })
    await screen.findByText('Connected room')
    await screen.findByText('Teams')
    await waitFor(() => expect(listTeams).toHaveBeenCalled())

    // Teams arrive over the window event (fire-and-forget bridge call).
    await act(async () => {
      window.dispatchEvent(new CustomEvent('runspec:teams', { detail: { teams: [
        { name: 'Default', slug: 'default' }, { name: 'HR', slug: 'hr' },
      ] } }))
    })
    await screen.findByText('HR')
    expect(screen.getByText('protected')).toBeTruthy()

    await act(async () => {
      await user.type(screen.getByPlaceholderText('New team name (e.g. HR)'), 'Finance')
      await user.click(screen.getByRole('button', { name: /Add/ }))
    })
    await waitFor(() => expect(addTeam).toHaveBeenCalledWith('Finance'))
    // The self-service catalogue is still there below.
    expect(screen.getByText('Self-service catalogue')).toBeTruthy()
  })
})

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { TerminalsModal } from './TerminalsModal'
import { addToSelection, hostsInGroup, hostsInProfile, readLayoutPref, writeLayoutPref } from './terminals'
import type { JumpHost } from '../bridge'

const { launchTerminals, getJumpHosts, getProfiles } = vi.hoisted(() => ({
  launchTerminals: vi.fn(),
  getJumpHosts: vi.fn(),
  getProfiles: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, prop) =>
        prop === 'launch_terminals' ? launchTerminals
          : prop === 'get_jump_hosts' ? getJumpHosts
            : prop === 'get_profiles' ? getProfiles
              : vi.fn(),
    },
  ),
}))

const HOSTS: JumpHost[] = [
  { name: 'web-1', hostname: 'web-1', group: 'Prod', profiles: ['ops'] },
  { name: 'web-2', hostname: 'web-2', group: 'Prod' },
  { name: 'db-1', hostname: 'db-1', group: 'Data', profiles: ['ops'] },
  { name: 'lab', hostname: 'lab' },
]

describe('terminal helpers', () => {
  it('picks hosts by group and profile in config order', () => {
    expect(hostsInGroup(HOSTS, 'Prod')).toEqual(['web-1', 'web-2'])
    expect(hostsInGroup(HOSTS, '')).toEqual(['lab'])
    expect(hostsInProfile(HOSTS, 'ops')).toEqual(['web-1', 'db-1'])
  })

  it('adds to a selection without duplicates, keeping order', () => {
    expect(addToSelection(['db-1'], ['web-1', 'db-1'])).toEqual(['db-1', 'web-1'])
  })

  it('remembers the layout, defaulting to grid', () => {
    localStorage.clear()
    expect(readLayoutPref()).toBe('grid')
    writeLayoutPref('columns')
    expect(readLayoutPref()).toBe('columns')
  })

  it('carries an old "tiling off" preference over as no arrangement', () => {
    localStorage.clear()
    localStorage.setItem('rs.terminals.tile', '0')
    expect(readLayoutPref()).toBe('none')
  })
})

describe('TerminalsModal', () => {
  beforeEach(() => {
    localStorage.clear()
    launchTerminals.mockReset().mockResolvedValue({ opened: ['web-1', 'web-2'], errors: [], tiling: true })
    getJumpHosts.mockReset().mockResolvedValue(HOSTS)
    getProfiles.mockReset().mockResolvedValue([{ name: 'ops' }])
  })

  it('opens the pre-selected hosts with tiling on by default', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<TerminalsModal open initial={['web-1', 'web-2']} connected={['web-1']} onClose={onClose} />)
    await user.click(screen.getByRole('button', { name: /Open 2 terminals/ }))
    expect(launchTerminals).toHaveBeenCalledWith(['web-1', 'web-2'], true, 'grid')
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('adds every connected host and uses the remembered layout', async () => {
    writeLayoutPref('columns')
    const user = userEvent.setup()
    render(<TerminalsModal open initial={[]} connected={['web-1', 'db-1']} onClose={() => {}} />)
    const all = await screen.findByRole('button', { name: /All connected \(2\)/ })
    await user.click(all)
    await user.click(screen.getByRole('button', { name: /Open 2 terminals/ }))
    expect(launchTerminals).toHaveBeenCalledWith(['web-1', 'db-1'], true, 'columns')
  })

  it("turns tiling off for Don't arrange", async () => {
    writeLayoutPref('none')
    const user = userEvent.setup()
    render(<TerminalsModal open initial={['web-1', 'web-2']} connected={[]} onClose={() => {}} />)
    await user.click(screen.getByRole('button', { name: /Open 2 terminals/ }))
    expect(launchTerminals).toHaveBeenCalledWith(['web-1', 'web-2'], false)
    expect(readLayoutPref()).toBe('none')
  })

  it('keeps the dialog open and lists hosts that failed', async () => {
    launchTerminals.mockResolvedValue({
      opened: ['web-1'], errors: [{ host: 'web-2', error: 'SSH key file not found' }], tiling: false,
    })
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<TerminalsModal open initial={['web-1', 'web-2']} connected={[]} onClose={onClose} />)
    await user.click(screen.getByRole('button', { name: /Open 2 terminals/ }))
    expect(await screen.findByText(/1 host could not open/)).toBeTruthy()
    expect(screen.getByText(/SSH key file not found/)).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })
})

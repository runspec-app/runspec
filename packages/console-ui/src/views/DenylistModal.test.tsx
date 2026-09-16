/**
 * Tests for the Denylist modal (fleet kill switch, opened from the hosts
 * panel). Scoped to the connected hosts it is given: every status read and
 * mutation targets exactly those, and the picker narrows within them
 * client-side. Mocks the bridge to assert the payload shapes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { DenylistModal } from './DenylistModal'
import type { Host } from '../bridge'

const { manageDenylist, denylistStatus } = vi.hoisted(() => ({
  manageDenylist: vi.fn(),
  denylistStatus: vi.fn(),
}))

const CONNECTED: Host[] = [
  { name: 'local', connected: true, runnableCount: 0, groups: [] },
  { name: 'prod-1', connected: true, runnableCount: 3, groups: ['app'], group: 'Production' },
  { name: 'prod-2', connected: true, runnableCount: 1, groups: ['app'], group: 'Production' },
]
const SCOPE = ['local', 'prod-1', 'prod-2']

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (prop === 'manage_denylist') return manageDenylist
        if (prop === 'denylist_status') return denylistStatus
        return vi.fn()
      },
    },
  ),
}))

describe('DenylistModal', () => {
  beforeEach(() => {
    manageDenylist.mockReset().mockResolvedValue('runspec deny all — 1/1 venv(s) ok across 1 host(s):\n✓ prod-1/app: ok')
    denylistStatus.mockReset().mockResolvedValue([
      { host: 'prod-1', venv: 'app', ok: true, present: true, mode: 'enforce', disabled: false, reason: '', rules: [{ runnables: ['deploy'], users: ['*'], hosts: ['*'], reason: 'freeze' }] },
      { host: 'prod-2', venv: 'app', ok: true, present: true, mode: 'enforce', disabled: true, reason: 'freeze', rules: [] },
    ])
  })

  it('queries the connected scope once on open and filters client-side when hosts are picked', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    await screen.findByText('1 rule')
    await screen.findByText('ALL')
    expect(denylistStatus).toHaveBeenCalledTimes(1)
    expect(denylistStatus.mock.calls[0][0]).toEqual(SCOPE)

    // Pick prod-1 in the target picker → the table narrows, no new query.
    const user = userEvent.setup()
    const picker = screen.getAllByRole('combobox')[0]  // Target hosts (the mode select comes later)
    await act(async () => {
      await user.click(picker)
      await user.click(await screen.findByTitle('prod-1 (Production)'))
    })
    await waitFor(() => expect(screen.queryByText('ALL')).toBeNull())
    expect(screen.getByText('1 rule')).toBeTruthy()
    expect(denylistStatus).toHaveBeenCalledTimes(1)

    // A mutation still sends the picked targets and re-reads the fleet after.
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Apply mode' }))
    })
    await waitFor(() => expect(manageDenylist).toHaveBeenCalledTimes(1))
    expect(manageDenylist.mock.calls[0][0]).toEqual({ action: 'mode', setting: 'enforce', targets: ['prod-1'] })
    await waitFor(() => expect(denylistStatus).toHaveBeenCalledTimes(2))
    expect(denylistStatus.mock.calls[1][0]).toEqual(SCOPE)
  })

  it('loads the status table on mount and refresh re-reads it', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    await screen.findByText('Kill switch')

    // status table populated on mount (one rule on prod-1/app)
    await waitFor(() => expect(denylistStatus).toHaveBeenCalled())
    await screen.findByText('1 rule')

    const calls = denylistStatus.mock.calls.length
    const user = userEvent.setup()
    await act(async () => {
      await user.click(screen.getByRole('button', { name: /Refresh status/ }))
    })
    await waitFor(() => expect(denylistStatus.mock.calls.length).toBeGreaterThan(calls))
    const lastCall = denylistStatus.mock.calls[denylistStatus.mock.calls.length - 1]
    expect(lastCall[0]).toEqual(SCOPE)  // always the connected scope, never the whole fleet
  })

  it('apply mode sends the selected enforcement setting', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    await screen.findByText('Kill switch')

    const user = userEvent.setup()
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Apply mode' }))
    })

    await waitFor(() => expect(manageDenylist).toHaveBeenCalledTimes(1))
    expect(manageDenylist.mock.calls[0][0]).toEqual({ action: 'mode', setting: 'enforce', targets: SCOPE })
  })

  it('add deny rule passes the glob lists', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    await screen.findByText('Kill switch')

    const user = userEvent.setup()
    await act(async () => {
      await user.click(screen.getByRole('button', { name: /Add deny rule/ }))
    })

    await waitFor(() => expect(manageDenylist).toHaveBeenCalledTimes(1))
    const payload = manageDenylist.mock.calls[0][0]
    expect(payload.action).toBe('add')
    expect(payload).toMatchObject({ runnable: [], user: [], host: [], targets: SCOPE })
  })
})

describe('DenylistModal scope', () => {
  beforeEach(() => {
    manageDenylist.mockReset().mockResolvedValue('ok')
    denylistStatus.mockReset().mockResolvedValue([])
  })

  it('does not query when nothing is connected and says so', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={[]} />)
    await screen.findByText('Kill switch')
    await screen.findByText('No connected hosts.')
    expect(denylistStatus).not.toHaveBeenCalled()
  })

  it('does not query while closed', async () => {
    render(<DenylistModal open={false} onClose={() => {}} hosts={CONNECTED} />)
    await new Promise(r => setTimeout(r, 20))
    expect(denylistStatus).not.toHaveBeenCalled()
  })
})

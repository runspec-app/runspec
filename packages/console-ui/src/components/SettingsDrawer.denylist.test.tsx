/**
 * Tests for the Denylist settings tab (fleet kill switch). Mocks the bridge so
 * we assert on the payload shape passed to manage_denylist and that its summary
 * is rendered.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { DenylistTab } from './SettingsDrawer'

const { getHosts, manageDenylist, denylistStatus } = vi.hoisted(() => ({
  getHosts: vi.fn(),
  manageDenylist: vi.fn(),
  denylistStatus: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (prop === 'get_hosts') return getHosts
        if (prop === 'manage_denylist') return manageDenylist
        if (prop === 'denylist_status') return denylistStatus
        return vi.fn()
      },
    },
  ),
}))

describe('DenylistTab', () => {
  beforeEach(() => {
    getHosts.mockReset().mockResolvedValue([
      { name: 'local', connected: true, runnableCount: 0, groups: [] },
      { name: 'prod-1', connected: true, runnableCount: 3, groups: ['app'], group: 'Production' },
    ])
    manageDenylist.mockReset().mockResolvedValue('runspec deny all — 1/1 venv(s) ok across 1 host(s):\n✓ prod-1/app: ok')
    denylistStatus.mockReset().mockResolvedValue([
      { host: 'prod-1', venv: 'app', ok: true, present: true, mode: 'enforce', disabled: false, reason: '', rules: [{ runnables: ['deploy'], users: ['*'], hosts: ['*'], reason: 'freeze' }] },
      { host: 'prod-2', venv: 'app', ok: true, present: true, mode: 'enforce', disabled: true, reason: 'freeze', rules: [] },
    ])
  })

  it('queries the whole fleet once on mount and filters client-side when hosts are picked', async () => {
    render(<DenylistTab />)
    await screen.findByText('1 rule')
    await screen.findByText('ALL')
    expect(denylistStatus).toHaveBeenCalledTimes(1)
    expect(denylistStatus.mock.calls[0][0]).toEqual([])

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
    expect(denylistStatus.mock.calls[1][0]).toEqual([])
  })

  it('loads the status table on mount and refresh re-reads it', async () => {
    render(<DenylistTab />)
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
    expect(lastCall[0]).toEqual([])  // targets empty ⇒ all
  })

  it('apply mode sends the selected enforcement setting', async () => {
    render(<DenylistTab />)
    await screen.findByText('Kill switch')

    const user = userEvent.setup()
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Apply mode' }))
    })

    await waitFor(() => expect(manageDenylist).toHaveBeenCalledTimes(1))
    expect(manageDenylist.mock.calls[0][0]).toEqual({ action: 'mode', setting: 'enforce', targets: [] })
  })

  it('add deny rule passes the glob lists', async () => {
    render(<DenylistTab />)
    await screen.findByText('Kill switch')

    const user = userEvent.setup()
    await act(async () => {
      await user.click(screen.getByRole('button', { name: /Add deny rule/ }))
    })

    await waitFor(() => expect(manageDenylist).toHaveBeenCalledTimes(1))
    const payload = manageDenylist.mock.calls[0][0]
    expect(payload.action).toBe('add')
    expect(payload).toMatchObject({ runnable: [], user: [], host: [], targets: [] })
  })
})

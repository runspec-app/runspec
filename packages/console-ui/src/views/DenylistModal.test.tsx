/**
 * Tests for the Denylist modal (fleet kill switch, opened from the hosts
 * panel). Scoped to the connected hosts it is given: every status read and
 * mutation targets exactly those, and the picker narrows within them
 * client-side. Mocks the bridge to assert the payload shapes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { DenylistModal, globMatch, unmatchedHostGlobs } from './DenylistModal'
import type { Host } from '../bridge'

const { manageDenylist, denylistStatus, pendingAdd } = vi.hoisted(() => ({
  manageDenylist: vi.fn(),
  denylistStatus: vi.fn(),
  pendingAdd: vi.fn(() => Promise.resolve([{ host: 'bastion', reason: '', since: 1 }])),
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
        if (prop === 'denylist_pending_add') return pendingAdd
        if (prop === 'denylist_pending') return () => Promise.resolve([])
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
    await screen.findByText('all blocked')
    expect(denylistStatus).toHaveBeenCalledTimes(1)
    expect(denylistStatus.mock.calls[0][0]).toEqual(SCOPE)

    // Pick prod-1 in the target picker → the table narrows, no new query.
    const user = userEvent.setup()
    const picker = screen.getAllByRole('combobox')[0]  // Target hosts (the mode select comes later)
    await act(async () => {
      await user.click(picker)
      await user.click(await screen.findByTitle('prod-1 (Production)'))
    })
    await waitFor(() => expect(screen.queryByText('all blocked')).toBeNull())
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
    await screen.findByText('Disable all runnables')

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
    await screen.findByText('Disable all runnables')

    const user = userEvent.setup()
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Apply mode' }))
    })

    await waitFor(() => expect(manageDenylist).toHaveBeenCalledTimes(1))
    expect(manageDenylist.mock.calls[0][0]).toEqual({ action: 'mode', setting: 'enforce', targets: SCOPE })
  })

  it('an empty deny rule asks like Disable all, because it blocks everything', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    await screen.findByText('Disable all runnables')

    const user = userEvent.setup()
    await act(async () => {
      await user.click(screen.getByRole('button', { name: /Add deny rule/ }))
    })
    expect(manageDenylist).not.toHaveBeenCalled()
    expect((await screen.findByTestId('risk-action')).textContent).toMatch(/Disable all runnables/)
    await act(async () => { await user.click(screen.getByTestId('risk-ok')) })
    await waitFor(() => expect(manageDenylist).toHaveBeenCalledTimes(1))
    expect(manageDenylist.mock.calls[0][0]).toMatchObject({ action: 'all', targets: SCOPE })
  })

  it('turning the mode off asks first; Cancel changes nothing', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    await screen.findByText('Disable all runnables')
    // Wait for the hosts' status: when it lands the control syncs to their
    // live mode, which would undo an Off picked before it.
    await screen.findByText('Now: enforce')
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByText('Off')) })
    await act(async () => { await user.click(screen.getByRole('button', { name: 'Apply mode' })) })
    expect((await screen.findByTestId('risk-action')).textContent).toMatch(/Mode: off/)
    await act(async () => { await user.click(screen.getByRole('button', { name: 'Cancel' })) })
    expect(manageDenylist).not.toHaveBeenCalled()
    // Cancel put the control back, so pick Off again.
    await act(async () => { await user.click(screen.getByText('Off')) })
    await act(async () => { await user.click(screen.getByRole('button', { name: 'Apply mode' })) })
    await act(async () => { await user.click(await screen.findByTestId('risk-ok')) })
    await waitFor(() => expect(manageDenylist).toHaveBeenCalledTimes(1))
    expect(manageDenylist.mock.calls[0][0]).toEqual({ action: 'mode', setting: 'off', targets: SCOPE })
  })
})

describe('DenylistModal scope', () => {
  beforeEach(() => {
    manageDenylist.mockReset().mockResolvedValue('ok')
    denylistStatus.mockReset().mockResolvedValue([])
  })

  it('does not query when nothing is connected and says so', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={[]} />)
    await screen.findByText('Disable all runnables')
    await screen.findByText('No connected hosts.')
    expect(denylistStatus).not.toHaveBeenCalled()
  })

  it('does not query while closed', async () => {
    render(<DenylistModal open={false} onClose={() => {}} hosts={CONNECTED} />)
    await new Promise(r => setTimeout(r, 20))
    expect(denylistStatus).not.toHaveBeenCalled()
  })
})

describe('DenylistModal per-rule actions and host globs', () => {
  const ROWS = [
    {
      host: 'prod-1', venv: 'app', ok: true, present: true, mode: 'enforce', disabled: false, reason: '',
      hostname: 'little-girl',
      rules: [
        { runnables: ['deploy'], users: ['*'], hosts: ['*'], reason: 'freeze' },
        { runnables: ['system-info'], users: ['*'], hosts: ['prod-1'], reason: '' },
      ],
    },
    { host: 'prod-2', venv: 'app', ok: true, present: true, mode: 'enforce', disabled: true, reason: 'freeze', rules: [], hostname: 'big-boy' },
    { host: 'prod-2', venv: 'old', ok: false, missing: true, error: 'no runspec at /opt/venvs/old/bin/runspec — venv not created yet, or remove the path from the host entry' },
  ]

  beforeEach(() => {
    manageDenylist.mockReset().mockResolvedValue('ok')
    denylistStatus.mockReset().mockResolvedValue(ROWS)
  })

  it('lists every rule in full on its own line (no ellipsis) with the machine hostname', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    expect(await screen.findByText('deploy — users *; hosts * — freeze')).toBeTruthy()
    expect(screen.getByText('system-info — users *; hosts prod-1')).toBeTruthy()
    expect(screen.getByText('little-girl')).toBeTruthy()
  })

  it('removes exactly one rule on its own host + venv', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    const user = userEvent.setup()
    const removeBtn = await screen.findByLabelText('Remove rule 2 on prod-1/app')
    await act(async () => { await user.click(removeBtn) })
    await act(async () => { await user.click(await screen.findByTestId('risk-ok')) })
    await waitFor(() => expect(manageDenylist).toHaveBeenCalledTimes(1))
    expect(manageDenylist.mock.calls[0][0]).toEqual({
      action: 'remove', targets: ['prod-1'], venv: 'app',
      runnable: ['system-info'], user: ['*'], host: ['prod-1'],
    })
  })

  it('re-enables a venv disabled with "Disable all" without clearing its rules', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    const user = userEvent.setup()
    const enableBtn = await screen.findByLabelText('Enable prod-2/app')
    await act(async () => { await user.click(enableBtn) })
    await act(async () => { await user.click(await screen.findByTestId('risk-ok')) })
    await waitFor(() => expect(manageDenylist).toHaveBeenCalledTimes(1))
    expect(manageDenylist.mock.calls[0][0]).toEqual({ action: 'enable', targets: ['prod-2'], venv: 'app' })
  })

  it("Disable all names the disconnected hosts it can't reach", async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} unreachable={['bastion', 'uat-db-01']} />)
    const user = userEvent.setup()
    await screen.findByText('little-girl')
    await act(async () => { await user.click(screen.getByRole('button', { name: /Disable all runnables/ })) })
    expect((await screen.findByTestId('denylist-unreachable')).textContent).toContain('2 disconnected hosts (bastion, uat-db-01)')
    // Confirming queues the block for them, and the window lists it as pending.
    await act(async () => { await user.click(screen.getByTestId('risk-ok')) })
    await waitFor(() => expect(pendingAdd).toHaveBeenCalledWith(['bastion', 'uat-db-01'], ''))
    expect((await screen.findByTestId('denylist-pending')).textContent).toContain('bastion')
  })

  it('cancelling "turn off" puts the mode control back to what the hosts are doing', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    const user = userEvent.setup()
    await screen.findByText('little-girl')
    expect(screen.getByTestId('denylist-live-mode').textContent).toBe('Now: enforce')
    await act(async () => { await user.click(screen.getByText('Off')) })
    await act(async () => { await user.click(screen.getByRole('button', { name: 'Apply mode' })) })
    await act(async () => { await user.click(await screen.findByRole('button', { name: 'Cancel' })) })
    await waitFor(() => expect((screen.getByRole('radio', { name: 'Enforce' }) as HTMLInputElement).checked).toBe(true))
    expect(manageDenylist).not.toHaveBeenCalled()
  })

  it('"Re-enable all" undoes Disable all only; the rules stay', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    const user = userEvent.setup()
    await screen.findByText('little-girl')
    await act(async () => { await user.click(screen.getByRole('button', { name: /Re-enable all/ })) })
    expect((await screen.findByTestId('risk-action')).textContent).toContain('Other deny rules stay')
    await act(async () => { await user.click(screen.getByTestId('risk-ok')) })
    await waitFor(() => expect(manageDenylist).toHaveBeenCalledTimes(1))
    expect(manageDenylist.mock.calls[0][0]).toEqual({ action: 'enable', targets: SCOPE })
  })

  it('"Clear every rule…" lists the rules and needs "clear" typed', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    const user = userEvent.setup()
    await screen.findByText('little-girl')
    await act(async () => { await user.click(screen.getByRole('button', { name: /Clear every rule/ })) })
    expect((await screen.findByTestId('risk-action')).textContent).toContain('prod-1/app: deploy')
    expect((screen.getByTestId('risk-ok') as HTMLButtonElement).disabled).toBe(true)
    await act(async () => { await user.type(screen.getByLabelText('Type clear to confirm'), 'clear') })
    await act(async () => { await user.click(screen.getByTestId('risk-ok')) })
    await waitFor(() => expect(manageDenylist).toHaveBeenCalledTimes(1))
    expect(manageDenylist.mock.calls[0][0]).toEqual({ action: 'clear', targets: SCOPE })
  })

  it('shows a path with no runspec as "not installed", not an error', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    expect(await screen.findByText('not installed')).toBeTruthy()
    expect(screen.queryByText('error')).toBeNull()
  })

  it('warns when a host glob is a console host name rather than a machine hostname', async () => {
    render(<DenylistModal open onClose={() => {}} hosts={CONNECTED} />)
    await screen.findByText('little-girl')
    // antd tags mode: open, type a search value, Enter turns it into a tag.
    const hostGlobs = screen.getAllByRole('combobox')[3]  // targets, runnables, users, hosts
    act(() => {
      fireEvent.mouseDown(hostGlobs)
      fireEvent.change(hostGlobs, { target: { value: 'prod-1' } })
    })
    act(() => { fireEvent.keyDown(hostGlobs, { key: 'Enter', code: 'Enter', keyCode: 13 }) })
    const warn = await screen.findByTestId('denylist-host-glob-warning')
    expect(warn.textContent).toContain('matches none of the targeted machines')
    expect(warn.textContent).toContain("console's name for a host")
  })
})

describe('glob helpers', () => {
  it('matches fnmatch-style globs', () => {
    expect(globMatch('little-*', 'little-girl')).toBe(true)
    expect(globMatch('l?ttle-girl', 'little-girl')).toBe(true)
    expect(globMatch('deb[1-3]', 'deb2')).toBe(true)
    expect(globMatch('deb[!1-3]', 'deb2')).toBe(false)
    expect(globMatch('a.b', 'axb')).toBe(false)
    expect(globMatch('deb2-alice', 'little-girl')).toBe(false)
  })

  it('reports only globs that match no known hostname, and nothing when none are known', () => {
    expect(unmatchedHostGlobs(['deb2-alice', 'little-*'], ['little-girl'])).toEqual(['deb2-alice'])
    expect(unmatchedHostGlobs(['deb2-alice'], [])).toEqual([])
  })
})

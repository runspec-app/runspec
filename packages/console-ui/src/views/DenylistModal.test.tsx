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
    const confirm = await screen.findByText('Remove', { selector: 'button span' })
    await act(async () => { await user.click(confirm) })
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
    const [confirm] = await screen.findAllByText('Enable', { selector: '.ant-popover button span' })
    await act(async () => { await user.click(confirm) })
    await waitFor(() => expect(manageDenylist).toHaveBeenCalledTimes(1))
    expect(manageDenylist.mock.calls[0][0]).toEqual({ action: 'enable', targets: ['prod-2'], venv: 'app' })
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

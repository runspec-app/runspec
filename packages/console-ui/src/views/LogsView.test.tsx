/**
 * LogsView: renders one entry per venv plus the separate per-user "~/logs" home
 * entry (badged "home"), from a single logs_status call per host; and surfaces a
 * failed prune/compact inside the modal rather than only as a toast.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { LogsView } from './LogsView'

const { logsStatus, logsPrune, shareVenvLogs, venvOwner } = vi.hoisted(() => ({
  logsStatus: vi.fn(),
  logsPrune: vi.fn(),
  shareVenvLogs: vi.fn(),
  venvOwner: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) =>
      p === 'logs_status' ? logsStatus
      : p === 'logs_prune' ? logsPrune
      : p === 'share_venv_logs' ? shareVenvLogs
      : p === 'venv_owner' ? venvOwner
      : vi.fn(),
  }),
}))

const PROPS = {
  selectedHost: 'local',
  activeScope: [] as string[],
  onScopeToggle: () => {},
  active: true,
}

beforeEach(() => {
  logsStatus.mockReset().mockResolvedValue([
    {
      group: 'ops-tools', ok: true, dirs: ['/opt/venvs/ops-tools/logs'],
      total_bytes: 900, total_files: 4,
      runnables: [{ runnable: 'backup', per_run_files: 3, archives: 1, total_bytes: 900, oldest: null, newest: '2026-06-01T00:00:00Z' }],
    },
    {
      group: '~/logs', ok: true, dirs: ['~/logs'],
      total_bytes: 20, total_files: 1,
      runnables: [{ runnable: 'other', per_run_files: 1, archives: 0, total_bytes: 20, oldest: null, newest: '2026-06-01T00:00:00Z' }],
    },
  ])
  logsPrune.mockReset()
  shareVenvLogs.mockReset()
  // Default: no owner pre-fill (owner empty) so "Run as" stays blank unless a test
  // opts in — keeps the existing arg assertions stable.
  venvOwner.mockReset().mockResolvedValue({ ok: true, owner: '', self: 'operator' })
})

describe('LogsView', () => {
  it('renders a per-venv entry and the separate ~/logs home entry', async () => {
    render(<LogsView {...PROPS} />)
    // venv entry + its runnable
    expect(await screen.findByText('ops-tools')).toBeTruthy()
    expect(screen.getByText('backup')).toBeTruthy()
    // the home entry, badged "home" (not "shared" — ~/logs is the per-user
    // fallback, the venv dirs are the shared ones), with its own runnable
    expect(screen.getByText('home')).toBeTruthy()
    expect(screen.getByText('other')).toBeTruthy()
    // one status call for the host (not one per venv)
    expect(logsStatus).toHaveBeenCalledWith('local')
    expect(logsStatus).toHaveBeenCalledTimes(1)
  })

  it('surfaces a failed prune dry-run inside the modal, not just a toast', async () => {
    // A pre-0.56 remote runspec can't scope the prune and fails the arg safely;
    // the operator must see *why* it did nothing — in the modal, by the button.
    logsPrune.mockResolvedValue({
      ok: false,
      error: 'unknown argument: --scope — upgrade runspec to 0.56 on this host to prune from the console.',
    })
    render(<LogsView {...PROPS} />)
    await screen.findByText('ops-tools')

    // Open the prune modal for the first venv entry (both entries have a "Prune…").
    fireEvent.click(screen.getAllByRole('button', { name: /Prune…/ })[0])
    expect(await screen.findByText('Prune logs')).toBeTruthy()

    // Set a policy so Preview enables, then run the dry-run.
    fireEvent.change(screen.getByPlaceholderText('7d'), { target: { value: '90d' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview (dry-run)' }))

    // The error is shown in the modal (an Alert), not swallowed into a toast.
    expect(await screen.findByText(/upgrade runspec to 0\.56/)).toBeTruthy()
  })

  it('offers "Make shared" on a venv entry (not ~/logs) and calls share_venv_logs', async () => {
    shareVenvLogs.mockResolvedValue({ ok: true, venv: 'ops-tools', group: 'consoles', as_user: 'svc', output: '{}' })
    render(<LogsView {...PROPS} />)
    await screen.findByText('ops-tools')

    // The button is on the venv entry only — never the per-user ~/logs home entry
    // (which is always writable by its owner, so there's nothing to share).
    const buttons = screen.getAllByRole('button', { name: /Make shared/ })
    expect(buttons).toHaveLength(1)

    fireEvent.click(buttons[0])
    expect(await screen.findByText('Make logs shared')).toBeTruthy()

    // Optional Unix group, then apply → shares this venv (no "Run as" here — the
    // owner pre-fill is empty in this test, so it runs as the login user).
    fireEvent.change(screen.getByPlaceholderText('consoles'), { target: { value: 'consoles' } })
    fireEvent.click(screen.getByRole('button', { name: /^Make shared$/ }))

    await waitFor(() => expect(shareVenvLogs).toHaveBeenCalledWith('local', 'ops-tools', 'consoles', undefined))
  })

  it('pre-fills "Run as" with the venv owner and passes it to share_venv_logs', async () => {
    // Owner differs from the login user → the field pre-fills with the owner and
    // the share escalates to it.
    venvOwner.mockResolvedValue({ ok: true, owner: 'svc', self: 'operator' })
    shareVenvLogs.mockResolvedValue({ ok: true, venv: 'ops-tools', group: '', as_user: 'svc', output: '{}' })
    render(<LogsView {...PROPS} />)
    await screen.findByText('ops-tools')

    fireEvent.click(screen.getAllByRole('button', { name: /Make shared/ })[0])
    await screen.findByText('Make logs shared')
    await waitFor(() =>
      expect((screen.getByPlaceholderText('(yourself)') as HTMLInputElement).value).toBe('svc'),
    )

    fireEvent.click(screen.getByRole('button', { name: /^Make shared$/ }))
    await waitFor(() => expect(shareVenvLogs).toHaveBeenCalledWith('local', 'ops-tools', undefined, 'svc'))
  })

  it('surfaces a share warning and keeps the modal open when the console user cannot write', async () => {
    venvOwner.mockResolvedValue({ ok: true, owner: 'svc', self: 'operator' })
    shareVenvLogs.mockResolvedValue({
      ok: true, venv: 'ops-tools', group: 'consoles', as_user: 'svc', output: '{}',
      warning: "Shared, but operator isn't in group 'consoles', so runs will still log to ~/logs.",
    })
    render(<LogsView {...PROPS} />)
    await screen.findByText('ops-tools')
    fireEvent.click(screen.getAllByRole('button', { name: /Make shared/ })[0])
    await screen.findByText('Make logs shared')
    fireEvent.click(screen.getByRole('button', { name: /^Make shared$/ }))
    // The share applied, but the warning is shown and the modal stays open so the
    // operator can act on it.
    expect(await screen.findByText(/still log to ~\/logs/)).toBeTruthy()
    expect(screen.getByText('Make logs shared')).toBeTruthy()
  })
})

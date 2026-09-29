/**
 * RunsIndexModal: loads the runs-index health via history_db_stats, pre-fills
 * "Run as" with the venv owner, drives history_db_maintain for reap/reindex/prune
 * (prune gated on a policy), and surfaces a failure inside the modal.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { RunsIndexModal, humanBytes, checkPruneAge, inboxSummary } from './RunsIndexModal'

const { stats, maintain, venvOwner } = vi.hoisted(() => ({
  stats: vi.fn(),
  maintain: vi.fn(),
  venvOwner: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) =>
      p === 'history_db_stats' ? stats
      : p === 'history_db_maintain' ? maintain
      : p === 'venv_owner' ? venvOwner
      : vi.fn(),
  }),
}))

const TARGET = { host: 'web-01', group: 'ops-tools' }

beforeEach(() => {
  stats.mockReset().mockResolvedValue({
    ok: true, group: 'ops-tools', exists: true, path: '/opt/venvs/ops-tools/logs/runspec_runs.db',
    size_bytes: 2048, rows: 12, by_status: { completed: 10, failed: 1, running: 1 },
    oldest: '2026-07-01T00:00:00+00:00', newest: '2026-09-01T00:00:00+00:00',
    running: 1, stale_running: 1, log_pruned: 0, schema_version: 2, write_error: '',
  })
  maintain.mockReset().mockResolvedValue({ ok: true, group: 'ops-tools', verb: 'reap', count: 1 })
  venvOwner.mockReset().mockResolvedValue({ ok: true, owner: 'svc', self: 'operator' })
})

describe('RunsIndexModal', () => {
  it('renders nothing without a target', () => {
    const { container } = render(<RunsIndexModal target={null} onClose={() => {}} />)
    expect(container.textContent).toBe('')
  })

  it('shows the index health and stale rows', async () => {
    render(<RunsIndexModal target={TARGET} onClose={() => {}} />)
    expect(await screen.findByText('/opt/venvs/ops-tools/logs/runspec_runs.db')).toBeTruthy()
    expect(screen.getByText('1 orphaned running row(s)')).toBeTruthy()
    expect(stats).toHaveBeenCalledWith('web-01', 'ops-tools')
  })

  it('reaps as the pre-filled venv owner and reloads stats', async () => {
    render(<RunsIndexModal target={TARGET} onClose={() => {}} />)
    await waitFor(() => expect((screen.getByPlaceholderText('(yourself)') as HTMLInputElement).value).toBe('svc'))
    fireEvent.click(screen.getByRole('button', { name: /Reap orphans/ }))
    await waitFor(() => expect(maintain).toHaveBeenCalledWith('web-01', 'ops-tools', 'reap', undefined, undefined, 'svc'))
    await waitFor(() => expect(stats).toHaveBeenCalledTimes(2))
  })

  it('disables prune until a policy is set', async () => {
    render(<RunsIndexModal target={TARGET} onClose={() => {}} />)
    await screen.findByText('/opt/venvs/ops-tools/logs/runspec_runs.db')
    const prune = screen.getByRole('button', { name: /Prune/ }) as HTMLButtonElement
    expect(prune.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('older than'), { target: { value: '30d' } })
    expect((screen.getByRole('button', { name: /Prune/ }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('rejects an age it cannot use, keeping prune disabled', async () => {
    render(<RunsIndexModal target={TARGET} onClose={() => {}} />)
    await screen.findByText('/opt/venvs/ops-tools/logs/runspec_runs.db')
    fireEvent.change(screen.getByLabelText('older than'), { target: { value: 'older than 3 days' } })
    expect(screen.getByText(/Use a number plus a unit/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('max rows'), { target: { value: '500' } })
    expect((screen.getByRole('button', { name: /Prune/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('prunes with both limits after confirming', async () => {
    maintain.mockResolvedValue({ ok: true, group: 'ops-tools', verb: 'prune', count: 3 })
    render(<RunsIndexModal target={TARGET} onClose={() => {}} />)
    await waitFor(() => expect((screen.getByPlaceholderText('(yourself)') as HTMLInputElement).value).toBe('svc'))
    fireEvent.change(screen.getByLabelText('older than'), { target: { value: '3d' } })
    fireEvent.change(screen.getByLabelText('max rows'), { target: { value: '1000' } })
    fireEvent.click(screen.getByRole('button', { name: /Prune/ }))
    expect(await screen.findByText(/older than 3 days, then all but the newest 1,000/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /OK|Yes/ }))
    await waitFor(() => expect(maintain).toHaveBeenCalledWith('web-01', 'ops-tools', 'prune', '3d', 1000, 'svc'))
  })

  it('surfaces a failure inside the modal', async () => {
    maintain.mockResolvedValue({ ok: false, group: 'ops-tools', error: 'needs runspec >= 0.62.0' })
    render(<RunsIndexModal target={TARGET} onClose={() => {}} />)
    await screen.findByText('/opt/venvs/ops-tools/logs/runspec_runs.db')
    fireEvent.click(screen.getByRole('button', { name: /Reindex/ }))
    expect(await screen.findByText('needs runspec >= 0.62.0')).toBeTruthy()
  })

  it('checks prune ages like core (day-granular)', () => {
    expect(checkPruneAge('')).toEqual({ days: null })
    expect(checkPruneAge('30d').days).toBe(30)
    expect(checkPruneAge('2w').days).toBe(14)
    expect(checkPruneAge('72h').days).toBe(3)
    expect(checkPruneAge('12h').error).toMatch(/0 days/)
    expect(checkPruneAge('3 days').error).toBeTruthy()
  })

  it('formats sizes', () => {
    expect(humanBytes(0)).toBe('0 B')
    expect(humanBytes(2048)).toBe('2.0 KB')
  })
})

describe('logs inbox', () => {
  it('summarises the inbox and flags a problem', () => {
    expect(inboxSummary({ state: 'ok', files: 3, running: 1, finished: 2 })).toEqual({ ok: true, text: '3 pending file(s), 1 running, 2 awaiting sweep' })
    expect(inboxSummary({ state: 'missing' })?.ok).toBe(false)
    expect(inboxSummary({ state: 'unsupported' })).toBeNull()
    expect(inboxSummary(null)).toBeNull()
  })

  it('shows the inbox row and sweeps it', async () => {
    stats.mockResolvedValue({
      ok: true, group: 'ops-tools', exists: true, path: '/opt/venvs/ops-tools/logs/runspec_runs.db',
      rows: 1, by_status: { completed: 1 }, inbox: { state: 'ok', files: 2, finished: 2 },
    })
    maintain.mockResolvedValue({ ok: true, group: 'ops-tools', verb: 'sweep', count: 2, as_user: 'svc' })
    render(<RunsIndexModal target={TARGET} onClose={() => {}} />)
    expect(await screen.findByText('2 pending file(s), 2 awaiting sweep')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Sweep inbox/ }))
    await waitFor(() => expect(maintain).toHaveBeenCalledWith('web-01', 'ops-tools', 'sweep', undefined, undefined, 'svc'))
  })

  it('offers no sweep for ~/logs', async () => {
    render(<RunsIndexModal target={{ host: 'web-01', group: '~/logs' }} onClose={() => {}} />)
    await screen.findByText('/opt/venvs/ops-tools/logs/runspec_runs.db')
    expect(screen.queryByRole('button', { name: /Sweep inbox/ })).toBeNull()
  })
})

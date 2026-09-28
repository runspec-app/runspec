/**
 * RunsIndexModal: loads the runs-index health via history_db_stats, pre-fills
 * "Run as" with the venv owner, drives history_db_maintain for reap/reindex/prune
 * (prune gated on a policy), and surfaces a failure inside the modal.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { RunsIndexModal, humanBytes } from './RunsIndexModal'

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

  it('surfaces a failure inside the modal', async () => {
    maintain.mockResolvedValue({ ok: false, group: 'ops-tools', error: 'needs runspec >= 0.62.0' })
    render(<RunsIndexModal target={TARGET} onClose={() => {}} />)
    await screen.findByText('/opt/venvs/ops-tools/logs/runspec_runs.db')
    fireEvent.click(screen.getByRole('button', { name: /Reindex/ }))
    expect(await screen.findByText('needs runspec >= 0.62.0')).toBeTruthy()
  })

  it('formats sizes', () => {
    expect(humanBytes(0)).toBe('0 B')
    expect(humanBytes(2048)).toBe('2.0 KB')
  })
})

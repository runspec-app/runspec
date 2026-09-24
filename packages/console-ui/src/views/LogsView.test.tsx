/**
 * LogsView: renders one entry per venv plus the separate shared "~/logs" home
 * entry (badged "shared"), from a single logs_status call per host.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

import { LogsView } from './LogsView'

const { logsStatus } = vi.hoisted(() => ({ logsStatus: vi.fn() }))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => (p === 'logs_status' ? logsStatus : vi.fn()),
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
})

describe('LogsView', () => {
  it('renders a per-venv entry and the separate shared ~/logs entry', async () => {
    render(<LogsView {...PROPS} />)
    // venv entry + its runnable
    expect(await screen.findByText('ops-tools')).toBeTruthy()
    expect(screen.getByText('backup')).toBeTruthy()
    // the home entry, badged "shared", with its own runnable
    expect(screen.getByText('shared')).toBeTruthy()
    expect(screen.getByText('other')).toBeTruthy()
    // one status call for the host (not one per venv)
    expect(logsStatus).toHaveBeenCalledWith('local')
    expect(logsStatus).toHaveBeenCalledTimes(1)
  })
})

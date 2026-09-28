/**
 * SharedSchedulesView: renders each host venv's shared cron schedules from
 * shared_schedules (read-only), flags crontab state (installed / legacy / not
 * installed / unknown), lists venvs without runspec-scheduler, and shows the last
 * run's captured output on expand.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { SharedSchedulesView, crontabTag, whenLabel, relTime } from './SharedSchedulesView'
import type { SharedSchedule } from '../bridge'

const { shared } = vi.hoisted(() => ({ shared: vi.fn() }))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: (_t, p) => (p === 'shared_schedules' ? shared : vi.fn()) }),
}))

const base: SharedSchedule = {
  id: 'nightly', runnable: 'backup', enabled: true, when: { cron: '0 2 * * *' }, args: { target: 'prod' },
  run_as: 'svc', installable: true, not_installable_reason: null, installed: true, line: 'exec',
  next_run: null, runs: [], last_output: null,
}

beforeEach(() => {
  shared.mockReset().mockResolvedValue([
    {
      group: 'ops', available: true, ok: true,
      status: {
        ok: true, venv: 'ops', config: '/opt/venvs/ops/runspec_scheduler.toml', backend: 'cron', errors: [],
        stale: [{ id: 'gone', user: 'svc' }],
        schedules: [{
          ...base,
          runs: [{ run_id: 'r1', status: 'failed', exit_code: 2, start_ts: new Date().toISOString(), duration_ms: 1500, trigger: 'schedule' }],
          last_output: { stdout: 'copied 3 files\n', stderr: 'disk full\n', truncated: false, errors: ['upload failed'], result: { message: 'backup failed — exit 2 in 1.50s' } },
        }],
      },
    },
    { group: 'web', available: false, ok: true },
    { group: 'api', available: true, ok: false, error: "needs runspec-scheduler >= 0.3.0" },
  ])
})

describe('SharedSchedulesView', () => {
  it('lists schedules per venv with crontab state and the last run', async () => {
    render(<SharedSchedulesView host="prod-1" />)
    expect(await screen.findByText('nightly')).toBeTruthy()
    expect(shared).toHaveBeenCalledWith('prod-1')
    expect(screen.getByText('installed')).toBeTruthy()
    expect(screen.getByText('exit 2')).toBeTruthy()
    expect(screen.getByText(/No runspec-scheduler in: web/)).toBeTruthy()
    expect(screen.getByText(/needs runspec-scheduler >= 0.3.0/)).toBeTruthy()
    expect(screen.getByText(/crontab line\(s\) no longer in the config/)).toBeTruthy()
  })

  it('shows the captured output on expand', async () => {
    render(<SharedSchedulesView host="prod-1" />)
    await screen.findByText('nightly')
    fireEvent.click(screen.getByRole('button', { name: /expand row/i }))
    expect(await screen.findByText('copied 3 files')).toBeTruthy()
    expect(screen.getByText('disk full')).toBeTruthy()
    expect(screen.getByText('upload failed')).toBeTruthy()
  })

  it('shows the subcommand next to the runnable', async () => {
    shared.mockResolvedValue([{
      group: 'ops', available: true, ok: true,
      status: { ok: true, venv: 'ops', config: 'c', backend: 'cron', errors: [], stale: [], schedules: [{ ...base, runnable: 'filesystem', command: ['list-directory'], args: {} }] },
    }])
    render(<SharedSchedulesView host="h" />)
    expect(await screen.findByText('filesystem list-directory')).toBeTruthy()
  })

  it('shows an empty state when no venv has the scheduler', async () => {
    shared.mockResolvedValue([{ group: 'web', available: false, ok: true }])
    render(<SharedSchedulesView host="h" />)
    await waitFor(() => expect(screen.getByText(/No venv on h has runspec-scheduler installed/)).toBeTruthy())
  })
})

describe('helpers', () => {
  it('crontabTag covers each state', () => {
    expect(crontabTag(base).label).toBe('installed')
    expect(crontabTag({ ...base, line: 'legacy' }).label).toBe('legacy line')
    expect(crontabTag({ ...base, installed: false }).label).toBe('not installed')
    expect(crontabTag({ ...base, installed: null }).label).toBe('unknown')
    expect(crontabTag({ ...base, installable: false, not_installable_reason: 'interval' }).label).toBe('not installable')
  })

  it('whenLabel and relTime', () => {
    expect(whenLabel({ when: { cron: '0 2 * * *', timezone: 'UTC' } })).toBe('0 2 * * * (UTC)')
    expect(whenLabel({ when: { every: '15m' } })).toBe('every 15m')
    const now = Date.parse('2026-09-28T12:00:00Z')
    expect(relTime('2026-09-28T11:00:00Z', now)).toBe('1h ago')
    expect(relTime('2026-09-28T12:05:00Z', now)).toBe('in 5m')
    expect(relTime(null, now)).toBe('—')
  })
})

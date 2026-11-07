/**
 * The Schedules tab's option cards (All / This console / Host cron / Host daemon)
 * and the All schedules list behind them: counts on the cards, the card picks the
 * source, problems are counted amber, filters narrow the list, and a last run
 * links to History.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'

import { SchedulesView, _resetHostReadCache } from './SchedulesView'
import type { Host, Schedule, SharedSchedule, SharedSchedulesVenv } from '../bridge'

const { api } = vi.hoisted(() => ({ api: {} as Record<string, ReturnType<typeof vi.fn>> }))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p: string) => (api[p] ??= vi.fn(() => Promise.resolve([]))),
  }),
}))

const sched = (over: Partial<SharedSchedule> = {}): SharedSchedule => ({
  id: 'nightly', runnable: 'backup', enabled: true, when: { cron: '0 2 * * *' }, args: { target: 'prod' },
  run_as: 'svc', installable: true, not_installable_reason: null, installed: true, line: 'exec',
  next_run: new Date(Date.now() + 3600_000).toISOString(),
  runs: [{ run_id: 'r1', status: 'completed', exit_code: 0, start_ts: new Date(Date.now() - 600_000).toISOString(), duration_ms: 1200 }],
  last_output: null, ...over,
})

const VENVS: SharedSchedulesVenv[] = [
  {
    host: 'web-1', group: 'ops', available: true, ok: true, owner: 'svc',
    status: { ok: true, venv: 'ops', config: 'c', backend: 'cron', errors: [], stale: [], schedules: [sched(), sched({ id: 'report', runnable: 'report', installed: false })] },
  },
  {
    host: 'web-1', group: 'jobs', available: true, ok: true, owner: 'svc-jobs',
    status: {
      ok: true, venv: 'jobs', config: 'c', backend: 'daemon', errors: [], stale: [],
      daemon: { state: 'running', pid: 4121, uptime_s: 3600, heartbeat_age_s: 4 },
      schedules: [sched({ id: 'queue-poll', runnable: 'check-queue', line: 'daemon', when: { every: '5m' } })],
    },
  },
]

const CONSOLE: Schedule[] = [{ id: 'rs-1', action: 'runnable', runnable: 'disk-usage', host: 'web-1', when: { cron: '0 * * * *' }, enabled: true } as Schedule]

const HOSTS: Host[] = [
  { name: 'local', connected: true, runnableCount: 0, groups: [] },
  { name: 'web-1', connected: true, runnableCount: 0, groups: [], group: 'prod' },
  { name: 'db-1', connected: false, runnableCount: 0, groups: [] },
]

beforeEach(() => {
  localStorage.clear()
  _resetHostReadCache()
  for (const k of Object.keys(api)) delete api[k]
  api.get_schedules = vi.fn(() => Promise.resolve(CONSOLE))
  api.shared_schedules_all = vi.fn(() => Promise.resolve(VENVS))
})

const card = (scope: string) => document.querySelector(`[data-scope="${scope}"]`) as HTMLElement

async function renderView(extra: Partial<Parameters<typeof SchedulesView>[0]> = {}) {
  render(<SchedulesView hosts={HOSTS} runnables={[]} selectedHost="local" {...extra} />)
  await waitFor(() => expect(api.shared_schedules_all).toHaveBeenCalled(), { timeout: 3000 })
  await screen.findByText('nightly', undefined, { timeout: 3000 })
}

describe('Schedules option cards', () => {
  it('reads local + connected hosts and shows counts on each card', async () => {
    await renderView()
    expect(api.shared_schedules_all).toHaveBeenCalledWith(['local', 'web-1'])
    expect(within(card('all')).getByText('4 schedules')).toBeTruthy()
    expect(within(card('console')).getByText('1 schedule')).toBeTruthy()
    expect(within(card('cron')).getByText('2 schedules')).toBeTruthy()
    expect(within(card('daemon')).getByText('1 schedule')).toBeTruthy()
    // the not-installed cron schedule is a problem, counted on All and Host cron
    expect(within(card('cron')).getByLabelText('1 problem')).toBeTruthy()
    expect(within(card('all')).getByLabelText('1 problem')).toBeTruthy()
    expect(within(card('daemon')).queryByLabelText(/problem/)).toBeNull()
    expect(card('all').getAttribute('aria-checked')).toBe('true')
  })

  it('the Host daemon card shows the daemon strip and only daemon rows', async () => {
    await renderView()
    fireEvent.click(card('daemon'))
    expect(card('daemon').getAttribute('aria-checked')).toBe('true')
    expect(screen.getByText('scheduler running')).toBeTruthy()
    expect(screen.getByText('queue-poll')).toBeTruthy()
    expect(screen.queryByText('nightly')).toBeNull()
    expect(localStorage.getItem('runspec.schedules.scope')).toBe('daemon')
  })

  it('This console opens the console’s own schedules table', async () => {
    await renderView()
    fireEvent.click(card('console'))
    expect(await screen.findByText('New schedule')).toBeTruthy()
  })

  it('Only problems narrows the list', async () => {
    await renderView()
    fireEvent.click(screen.getByRole('switch', { name: 'Only problems' }))
    expect(screen.getAllByText('report').length).toBeGreaterThan(0)
    expect(screen.queryByText('nightly')).toBeNull()
  })

  it('a host: token narrows to that host', async () => {
    await renderView()
    fireEvent.change(screen.getByPlaceholderText(/Search id, runnable, args/), { target: { value: 'host:nope' } })
    expect(screen.queryByText('nightly')).toBeNull()
    expect(screen.getByText('No schedule matches these filters.')).toBeTruthy()
  })

  it('a last run links to History by its schedule label', async () => {
    const onOpenHistory = vi.fn()
    await renderView({ onOpenHistory })
    fireEvent.click(screen.getAllByRole('button', { name: 'Open in History' })[0])
    expect(onOpenHistory).toHaveBeenCalledWith(expect.stringMatching(/^sched:/))
  })

  it('shows "none yet" on Host daemon when no venv runs the daemon', async () => {
    api.shared_schedules_all = vi.fn(() => Promise.resolve([VENVS[0]]))
    await renderView()
    expect(within(card('daemon')).getByText('none yet')).toBeTruthy()
  })
})

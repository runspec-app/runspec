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

async function renderView(extra: Partial<Parameters<typeof SchedulesView>[0]> = {}, { firstVisit = false } = {}) {
  // A stored (empty) filter means the viewer has chosen, so the list isn't
  // narrowed to problems on open; firstVisit leaves it unset.
  if (!firstVisit) localStorage.setItem('runspec.schedules.allFilters', '{}')
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

  it('Only problems narrows the list, shown as a removable chip', async () => {
    await renderView()
    expect(screen.queryByTestId('filter-chips')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Filter' }))
    fireEvent.click(await screen.findByRole('switch', { name: 'Only problems' }))
    expect(screen.getAllByText('report').length).toBeGreaterThan(0)
    expect(screen.queryByText('nightly')).toBeNull()
    expect(screen.getByRole('button', { name: 'Filter, 1 active' })).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Remove filter only problems'))
    expect(screen.getByText('nightly')).toBeTruthy()
  })

  it('opens on just the problems the first time there are any', async () => {
    render(<SchedulesView hosts={HOSTS} runnables={[]} selectedHost="local" />)
    expect((await screen.findAllByText('report', undefined, { timeout: 3000 })).length).toBeGreaterThan(0)
    expect(within(screen.getByTestId('filter-chips')).getByText('only problems')).toBeTruthy()
    expect(screen.queryByText('nightly')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Show all' }))
    expect(screen.getByText('nightly')).toBeTruthy()
    // That choice sticks: the list isn't narrowed again.
    expect(JSON.parse(localStorage.getItem('runspec.schedules.allFilters')!).onlyProblems).toBe(false)
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

describe('who is handling a failing host schedule', () => {
  const failing = (state: 'failed' | 'handling') => sched({
    id: 'flaky', runnable: 'flaky-job',
    runs: [{ run_id: 'run-flaky-2', status: 'failed', exit_code: 3, start_ts: new Date(Date.now() - 600_000).toISOString() }],
    attention: {
      state, failures: 2, since: new Date(Date.now() - 86400_000).toISOString(), run_id: 'run-flaky-2',
      note: state === 'handling' ? { state, actor: 'ana', note: 'looking at the quota', ts: new Date(Date.now() - 300_000).toISOString() } : null,
    },
  })

  it('tick a failing row, say "I\'m on it", one call per venv with its run id', async () => {
    api.shared_schedules_all = vi.fn(() => Promise.resolve([{ ...VENVS[0], status: { ...VENVS[0].status!, schedules: [sched(), failing('failed')] } }]))
    api.host_schedule_note = vi.fn(() => Promise.resolve({ ok: true, notes: [{ state: 'handling', ts: 'x' }] }))
    await renderView()
    expect(screen.getByText('last run failed')).toBeTruthy()
    // only the failing row has a checkbox
    expect(screen.queryByLabelText('Select nightly')).toBeNull()
    fireEvent.click(screen.getByLabelText('Select flaky'))
    expect(screen.getByText('1 failing schedule selected')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Release' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: "I'm on it" }))
    fireEvent.change(await screen.findByLabelText('Note'), { target: { value: 'checking the disk' } })
    fireEvent.click(within(document.querySelector('.ant-modal') as HTMLElement).getByRole('button', { name: "I'm on it" }))
    await waitFor(() => expect(api.host_schedule_note).toHaveBeenCalledWith({ host: 'web-1', venv: 'ops', verb: 'claim', runs: ['run-flaky-2'], note: 'checking the disk' }))
  })

  it('a claimed failure shows who has it and offers Release; dealt with needs a note', async () => {
    api.shared_schedules_all = vi.fn(() => Promise.resolve([{ ...VENVS[0], status: { ...VENVS[0].status!, schedules: [failing('handling')] } }]))
    api.host_schedule_note = vi.fn(() => Promise.resolve({ ok: true, notes: [] }))
    render(<SchedulesView hosts={HOSTS} runnables={[]} selectedHost="local" />)
    expect(await screen.findByText('being handled', undefined, { timeout: 3000 })).toBeTruthy()
    expect(screen.getByText('ana: looking at the quota')).toBeTruthy()
    // being handled isn't a problem
    expect(within(card('cron')).queryByLabelText(/problem/)).toBeNull()
    fireEvent.click(screen.getByLabelText('Select flaky'))
    expect(screen.getByRole('button', { name: 'Release' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Dealt with…' }))
    const ok = await screen.findByRole('button', { name: 'Dealt with' })
    expect((ok as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'quota raised' } })
    fireEvent.click(ok)
    await waitFor(() => expect(api.host_schedule_note).toHaveBeenCalledWith(expect.objectContaining({ verb: 'resolve', note: 'quota raised' })))
  })
})

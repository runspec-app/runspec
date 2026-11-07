/**
 * Editing host schedules from the Schedules tab: the dialog offers the venvs
 * whose scheduler was read, the runnables of the chosen venv, never stores a
 * password, and saves through host_schedule_save; the list's row actions pause,
 * remove and sync a venv through the matching bridge calls.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { HostScheduleModal, hostScheduleArgs, suggestId, timeoutText } from './HostScheduleModal'
import { AllSchedulesView, keepPayload, needsSync } from './AllSchedulesView'
import { collectRows, type SchedulerVenv } from './allSchedules'
import type { ArgDef, Runnable, SharedSchedule, SharedSchedulesVenv } from '../bridge'

const { api } = vi.hoisted(() => ({ api: {} as Record<string, ReturnType<typeof vi.fn>> }))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: (_t, p: string) => (api[p] ??= vi.fn(() => Promise.resolve({ ok: true, warnings: [] }))) }),
}))

const arg = (over: Partial<ArgDef>): ArgDef => ({ name: 'x', type: 'str', required: false, ...over })

const VENVS: SchedulerVenv[] = [
  { key: 'web-1::ops', host: 'web-1', hostGroup: 'prod', venv: 'ops', backend: 'cron', owner: 'svc' },
  { key: 'web-1::jobs', host: 'web-1', hostGroup: 'prod', venv: 'jobs', backend: 'daemon', owner: 'svc-jobs' },
]

const RUNNABLES: Runnable[] = [
  {
    name: 'backup', group: 'ops', host: 'web-1',
    args: [arg({ name: 'target', required: true }), arg({ name: 'level', default: 'info' }), arg({ name: 'token', type: 'password', required: true })],
  },
  { name: 'report', group: 'jobs', host: 'web-1', args: [] },
]

const sched = (over: Partial<SharedSchedule> = {}): SharedSchedule => ({
  id: 'nightly', runnable: 'backup', enabled: true, when: { cron: '0 2 * * *' }, args: { target: 'prod' },
  run_as: 'svc', installable: true, not_installable_reason: null, installed: true, line: 'exec',
  next_run: null, runs: [], last_output: null, ...over,
})

beforeEach(() => {
  for (const k of Object.keys(api)) delete api[k]
  api.host_schedule_save = vi.fn(() => Promise.resolve({ ok: true, id: 'backup', warnings: [], sync: { installed: 1, skipped: [], users: ['svc'] } }))
})

describe('helpers', () => {
  it('stores only set, non-default, non-secret args', () => {
    const args = [arg({ name: 'a' }), arg({ name: 'b', default: 'x' }), arg({ name: 'p', type: 'password' }), arg({ name: 'f', type: 'flag' })]
    expect(hostScheduleArgs(args, { a: 'v', b: 'x', p: 'secret', f: false })).toEqual({ a: 'v' })
    expect(hostScheduleArgs(args, { a: '', b: 'y', f: true })).toEqual({ b: 'y', f: true })
  })

  it('suggests a free id and shows timeouts in their short form', () => {
    expect(suggestId('backup', ['run', 'now'], [])).toBe('backup-run-now')
    expect(suggestId('backup', [], ['backup', 'backup-2'])).toBe('backup-3')
    expect(timeoutText(21600, 'daemon')).toBe('')
    expect(timeoutText(21600, 'cron')).toBe('6h')
    expect(timeoutText(5400, 'cron')).toBe('90m')
    expect(timeoutText(0, 'daemon')).toBe('0')
    expect(timeoutText(null, 'cron')).toBe('')
  })

  it('keeps a schedule as is when pausing it', () => {
    const p = keepPayload(VENVS[1], sched({ when: { every: '5m' }, timeout: 600, catch_up: true, command: ['check'] }), false)
    expect(p).toEqual({
      host: 'web-1', venv: 'jobs', backend: 'daemon', id: 'nightly', original_id: 'nightly', runnable: 'backup',
      args: { target: 'prod' }, enabled: false, command: ['check'], every: '5m', timeout: '10m', catch_up: true,
    })
  })
})

describe('HostScheduleModal', () => {
  it('adds a schedule to the chosen venv without storing the password', async () => {
    const user = userEvent.setup()
    const onSaved = vi.fn()
    api.host_schedule_env_check = vi.fn(() => Promise.resolve({
      ok: true, host: 'web-1', runspec_path: '/v/ops/bin/runspec', path: '/v/ops/.runspec_env', exists: true, override: '',
      needs: [{ arg: 'token', command: '', password: true, secret: true, description: '', keys: ['RUNSPEC_BACKUP_ARG_TOKEN', 'BACKUP_TOKEN'], set_by: '', blank: [] }],
    }))
    const opened = vi.fn()
    window.addEventListener('runspec:open_env_files', opened)
    render(<HostScheduleModal open venvs={VENVS} runnables={RUNNABLES} idsByVenv={{}} editing={null} onClose={() => {}} onSaved={onSaved} />)
    // The cron venv is first: its runnable list holds backup, not report.
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Runnable' }))
    await waitFor(() => expect(document.querySelectorAll('.ant-select-item-option').length).toBe(1))
    fireEvent.click(document.querySelector('.ant-select-item-option') as HTMLElement)
    // The token can't ride the schedule: the panel says the host's env file lacks it.
    expect(await screen.findByText('1 credential not set on the host')).toBeTruthy()
    expect(api.host_schedule_env_check).toHaveBeenCalledWith({ host: 'web-1', venv: 'ops', runnable: 'backup', command: [] })
    expect(screen.getByText('BACKUP_TOKEN')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: /Open Env files/ }))
    expect((opened.mock.calls[0][0] as CustomEvent).detail).toEqual({ host: 'web-1', runspec_path: '/v/ops/bin/runspec' })
    window.removeEventListener('runspec:open_env_files', opened)
    expect((screen.getByLabelText('Schedule id') as HTMLInputElement).value).toBe('backup')

    // Required arg missing → no save.
    await user.click(screen.getByRole('button', { name: 'Add schedule' }))
    expect(await screen.findByText('Fill in the required arguments.')).toBeTruthy()
    expect(api.host_schedule_save).not.toHaveBeenCalled()

    const label = screen.getByText('Required')
    let box: HTMLElement | null = label.parentElement
    while (box && !box.querySelector('textarea, input')) box = box.parentElement
    await user.type(box!.querySelector('textarea, input') as HTMLElement, 'prod')
    await user.click(screen.getByRole('button', { name: 'Add schedule' }))
    await waitFor(() => expect(api.host_schedule_save).toHaveBeenCalled())
    expect(api.host_schedule_save.mock.calls[0][0]).toMatchObject({
      host: 'web-1', venv: 'ops', backend: 'cron', id: 'backup', runnable: 'backup',
      cron: '0 2 * * *', enabled: true, args: { target: 'prod' }, password_args: ['token'],
    })
    expect(onSaved).toHaveBeenCalled()
  })

  it('edits in place: the venv is fixed and the old id is sent', async () => {
    const user = userEvent.setup()
    render(<HostScheduleModal open venvs={VENVS} runnables={RUNNABLES} idsByVenv={{}}
      editing={{ venv: VENVS[0], schedule: sched({ when: { cron: '17 3 * * *' } }) }} onClose={() => {}} onSaved={() => {}} />)
    expect((screen.getByLabelText('Cron expression') as HTMLInputElement).value).toBe('17 3 * * *')
    await user.clear(screen.getByLabelText('Schedule id'))
    await user.type(screen.getByLabelText('Schedule id'), 'nightly-2')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.host_schedule_save).toHaveBeenCalled())
    expect(api.host_schedule_save.mock.calls[0][0]).toMatchObject({ id: 'nightly-2', original_id: 'nightly', cron: '17 3 * * *', args: { target: 'prod' } })
  })

  it('shows the scheduler refusal', async () => {
    api.host_schedule_save = vi.fn(() => Promise.resolve({ ok: false, error: "runnable 'report' is not installed", warnings: [] }))
    const user = userEvent.setup()
    render(<HostScheduleModal open venvs={VENVS} runnables={RUNNABLES} idsByVenv={{}}
      editing={{ venv: VENVS[1], schedule: sched({ runnable: 'report', args: {}, when: { every: '5m' } }) }} onClose={() => {}} onSaved={() => {}} />)
    expect((screen.getByRole('textbox', { name: 'Interval' }) as HTMLInputElement).value).toBe('5m')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText("runnable 'report' is not installed")).toBeTruthy()
  })
})

describe('AllSchedulesView host actions', () => {
  const venvs: SharedSchedulesVenv[] = [{
    host: 'web-1', group: 'ops', available: true, ok: true, owner: 'svc',
    status: {
      ok: true, venv: 'ops', config: 'c', backend: 'cron', errors: [],
      stale: [{ id: 'gone', user: 'svc', reason: 'not in the config' }],
      schedules: [sched(), sched({ id: 'report', runnable: 'report', installed: false })],
    },
  }]
  const collected = collectRows([], venvs)

  it('lists editable venvs and marks rows that a sync fixes', () => {
    expect(collected.venvs.map(v => `${v.key}:${v.backend}`)).toEqual(['web-1::ops:cron'])
    expect(collected.rows.filter(needsSync).map(r => r.id).sort()).toEqual(['gone', 'report'])
  })

  it('pauses, removes and syncs through the bridge, then re-reads', async () => {
    const onRefresh = vi.fn()
    api.host_schedule_remove = vi.fn(() => Promise.resolve({ ok: true, id: 'nightly', warnings: [] }))
    api.host_schedule_sync = vi.fn(() => Promise.resolve({ ok: true, warnings: [], sync: { installed: 2, skipped: [], users: ['svc'] } }))
    render(<AllSchedulesView scope="cron" collected={collected} loading={false} loaded updatedAt={null} hostCount={1}
      onRefresh={onRefresh} runnables={RUNNABLES} />)
    const row = (id: string) => screen.getByText(id).closest('tr') as HTMLElement

    fireEvent.click(within(row('nightly')).getByLabelText('Pause'))
    await waitFor(() => expect(api.host_schedule_save).toHaveBeenCalledWith(expect.objectContaining({ id: 'nightly', enabled: false, original_id: 'nightly' })))
    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1))

    fireEvent.click(within(row('nightly')).getByLabelText('Remove'))
    fireEvent.click(within(await waitFor(() => document.querySelector('.ant-popconfirm') as HTMLElement)).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(api.host_schedule_remove).toHaveBeenCalledWith({ host: 'web-1', venv: 'ops', backend: 'cron', id: 'nightly' }))

    fireEvent.click(within(row('gone')).getByLabelText('Sync crontab'))
    await waitFor(() => expect(api.host_schedule_sync).toHaveBeenCalledWith({ host: 'web-1', venv: 'ops' }))
    expect(screen.getByRole('button', { name: /New host schedule/ })).toBeTruthy()
  })

  it('runs a schedule now through the bridge', async () => {
    api.host_schedule_run_now = vi.fn(() => Promise.resolve({ ok: true, id: 'nightly', pid: 9, user: 'svc' }))
    render(<AllSchedulesView scope="cron" collected={collected} loading={false} loaded updatedAt={null} hostCount={1}
      onRefresh={() => {}} runnables={RUNNABLES} />)
    const row = screen.getByText('nightly').closest('tr') as HTMLElement
    fireEvent.click(within(row).getByLabelText('Run now'))
    const pop = await waitFor(() => document.querySelector('.ant-popconfirm') as HTMLElement)
    expect(within(pop).getByText(/as svc/)).toBeTruthy()
    fireEvent.click(within(pop).getByRole('button', { name: 'Run now' }))
    await waitFor(() => expect(api.host_schedule_run_now).toHaveBeenCalledWith({ host: 'web-1', venv: 'ops', id: 'nightly' }))
  })

  it('stays read-only without runnables', () => {
    render(<AllSchedulesView scope="cron" collected={collected} loading={false} loaded updatedAt={null} hostCount={1} onRefresh={() => {}} />)
    expect(screen.queryByLabelText('Pause')).toBeNull()
    expect(screen.queryByRole('button', { name: /New host schedule/ })).toBeNull()
    expect(screen.getByText(/Read-only/)).toBeTruthy()
  })
})

describe('AllSchedulesView host daemon', () => {
  const daemonVenvs = (state: 'running' | 'stopped'): SharedSchedulesVenv[] => [{
    host: 'job-1', group: 'jobs', available: true, ok: true, owner: 'svc-jobs',
    status: {
      ok: true, venv: 'jobs', config: 'c', backend: 'daemon', errors: [], stale: [], schedules: [],
      daemon: state === 'running' ? { state, pid: 41, version: '0.6.2', restart_needed: true } : { state },
      settings: { backend: 'daemon', timeout: 21600, misfire_grace: 300, catch_up_window: 86400 },
      keeper: [{ manager: 'systemd-user', where: 'unit' }],
      log: '/opt/venvs/jobs/logs/scheduler.log',
    },
  }]

  it('restarts a running daemon through the bridge, then re-reads', async () => {
    const onRefresh = vi.fn()
    api.host_scheduler_restart = vi.fn(() => Promise.resolve({ ok: true, manager: 'systemd-user', daemon: { state: 'running', pid: 77 }, warnings: [] }))
    render(<AllSchedulesView scope="daemon" collected={collectRows([], daemonVenvs('running'))} loading={false} loaded updatedAt={null} hostCount={1}
      onRefresh={onRefresh} runnables={RUNNABLES} />)
    expect(screen.getByText('restart needed')).toBeTruthy()
    expect(screen.getByText(/kept by systemd --user/)).toBeTruthy()
    expect(screen.getByText(/v0\.6\.2/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Restart/ }))
    fireEvent.click(within(await waitFor(() => document.querySelector('.ant-popconfirm') as HTMLElement)).getByRole('button', { name: 'Restart' }))
    await waitFor(() => expect(api.host_scheduler_restart).toHaveBeenCalledWith({ host: 'job-1', venv: 'jobs' }))
    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1))
  })

  it('offers Start for a stopped daemon', () => {
    render(<AllSchedulesView scope="daemon" collected={collectRows([], daemonVenvs('stopped'))} loading={false} loaded updatedAt={null} hostCount={1}
      onRefresh={() => {}} runnables={RUNNABLES} />)
    expect(screen.getByRole('button', { name: /Start/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Restart/ })).toBeNull()
  })

  it('shows the settings and saves only what changed', async () => {
    const onRefresh = vi.fn()
    api.host_scheduler_settings = vi.fn(() => Promise.resolve({ ok: true, settings: {} }))
    render(<AllSchedulesView scope="daemon" collected={collectRows([], daemonVenvs('running'))} loading={false} loaded updatedAt={null} hostCount={1}
      onRefresh={onRefresh} runnables={RUNNABLES} />)
    fireEvent.click(screen.getByRole('button', { name: /Settings/ }))
    const timeout = await screen.findByLabelText('Fire timeout') as HTMLInputElement
    expect(timeout.value).toBe('6h')
    expect((screen.getByLabelText('Misfire grace') as HTMLInputElement).value).toBe('5m')
    expect(screen.getByText('/opt/venvs/jobs/logs/scheduler.log')).toBeTruthy()
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save.hasAttribute('disabled')).toBe(true)
    fireEvent.change(timeout, { target: { value: '2h' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.host_scheduler_settings).toHaveBeenCalledWith({ host: 'job-1', venv: 'jobs', timeout: '2h' }))
    await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1))
  })

  it('has no daemon buttons when read-only', () => {
    render(<AllSchedulesView scope="daemon" collected={collectRows([], daemonVenvs('running'))} loading={false} loaded updatedAt={null} hostCount={1}
      onRefresh={() => {}} />)
    expect(screen.queryByRole('button', { name: /Restart/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Settings/ })).toBeNull()
  })
})

describe('AllSchedulesView daemon list folding', () => {
  const many: SharedSchedulesVenv[] = ['a', 'b', 'c', 'd', 'e'].map((h, i) => ({
    host: `job-${h}`, group: 'jobs', available: true, ok: true, owner: 'svc',
    status: {
      ok: true, venv: 'jobs', config: 'c', backend: 'daemon', errors: [], stale: [], schedules: [],
      daemon: i === 4 ? { state: 'stopped' } : { state: 'running', pid: 10 + i },
    },
  }))

  beforeEach(() => { try { localStorage.removeItem('runspec.schedules.daemonsOpen') } catch { /* none */ } })

  it('folds many daemons to a summary plus the ones that need a look, and remembers opening', () => {
    const { unmount } = render(<AllSchedulesView scope="daemon" collected={collectRows([], many)} loading={false} loaded updatedAt={null} hostCount={5} onRefresh={() => {}} />)
    const summary = screen.getByRole('button', { name: /5 scheduler daemons: 4 running, 1 stopped/ })
    expect(summary.getAttribute('aria-expanded')).toBe('false')
    expect(screen.getByText('job-e / jobs')).toBeTruthy()
    expect(screen.queryByText('job-a / jobs')).toBeNull()
    fireEvent.click(summary)
    expect(screen.getByText('job-a / jobs')).toBeTruthy()
    unmount()
    render(<AllSchedulesView scope="daemon" collected={collectRows([], many)} loading={false} loaded updatedAt={null} hostCount={5} onRefresh={() => {}} />)
    expect(screen.getByText('job-a / jobs')).toBeTruthy()
  })
})

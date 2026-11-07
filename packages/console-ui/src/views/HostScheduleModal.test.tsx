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
    args: [arg({ name: 'target', required: true }), arg({ name: 'level', default: 'info' }), arg({ name: 'token', type: 'password' })],
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
    render(<HostScheduleModal open venvs={VENVS} runnables={RUNNABLES} idsByVenv={{}} editing={null} onClose={() => {}} onSaved={onSaved} />)
    // The cron venv is first: its runnable list holds backup, not report.
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Runnable' }))
    await waitFor(() => expect(document.querySelectorAll('.ant-select-item-option').length).toBe(1))
    fireEvent.click(document.querySelector('.ant-select-item-option') as HTMLElement)
    expect(await screen.findByText(/token: not stored in a host schedule/)).toBeTruthy()
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

  it('stays read-only without runnables', () => {
    render(<AllSchedulesView scope="cron" collected={collected} loading={false} loaded updatedAt={null} hostCount={1} onRefresh={() => {}} />)
    expect(screen.queryByLabelText('Pause')).toBeNull()
    expect(screen.queryByRole('button', { name: /New host schedule/ })).toBeNull()
    expect(screen.getByText(/Read-only/)).toBeTruthy()
  })
})

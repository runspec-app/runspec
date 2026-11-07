/**
 * The Update venv dialog: venv choices come from paths hosts already list,
 * the preview reads each venv's state without changing it, per-host notes
 * follow the chosen options (log cleanup on an old runspec, "back to crontab"
 * with no scheduler), and only the ready hosts + chosen options are sent.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import {
  UpdateVenvModal,
  UPDATE_SCHEDULER_OPTIONS,
  buildUpdatePayload,
  hasWork,
  moveText,
  moveTotal,
  updateNotes,
  versionAtLeast,
  type UpdateForm,
} from './UpdateVenvModal'
import type { ProvisionOptions, VenvScheduleCounts, VenvUpdateRow } from '../bridge'

const { api } = vi.hoisted(() => ({
  api: {
    provision_options: vi.fn(),
    venv_update_preview: vi.fn(),
    venv_update_start: vi.fn(),
  } as Record<string, ReturnType<typeof vi.fn>>,
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: (_t, prop: string) => api[prop] ?? vi.fn().mockResolvedValue(undefined) }),
}))

const RP = '/opt/venvs/ops/bin/runspec'
const OPTS: ProvisionOptions = {
  hosts: [
    { name: 'web-01', connected: true, group: 'Web', profiles: [], runspec_paths: [RP] },
    { name: 'web-02', connected: true, group: 'Web', profiles: [], runspec_paths: [RP] },
    { name: 'db-01', connected: false, group: 'DB', profiles: [], runspec_paths: [RP] },
  ],
  groups: ['Web', 'DB'],
  profiles: [],
  repos: [],
  min_python: '3.10',
}

function row(over: Partial<VenvUpdateRow> = {}): VenvUpdateRow {
  return {
    host: 'web-01', runspec_path: RP, root: '/opt/venvs/ops', ok: true, kind: 'venv', owner: 'svc', user: 'deploy',
    size_kb: 1, logs_kb: 1, runnables: [], references: [], blockers: [],
    scheduler: false, scheduler_pid: 0, runspec_version: '0.79.0', inbox: false, retention: {}, backend: '',
    ...over,
  }
}

const FORM: UpdateForm = {
  upgradeRunspec: true, packages: [], repo: '', inbox: true,
  retention: { compact_after: '7d', delete_after: '90d' }, scheduler: '', become: 'sudo',
}

describe('update helpers', () => {
  it('compares versions', () => {
    expect(versionAtLeast('0.80.0', '0.80.0')).toBe(true)
    expect(versionAtLeast('0.81.2', '0.80.0')).toBe(true)
    expect(versionAtLeast('1.0', '0.80.0')).toBe(true)
    expect(versionAtLeast('0.79.9', '0.80.0')).toBe(false)
    expect(versionAtLeast('', '0.80.0')).toBe(false)
  })

  it('builds the payload and knows when there is nothing to do', () => {
    const t = [{ host: 'web-01', runspec_path: RP }]
    const p = buildUpdatePayload(t, { ...FORM, packages: [' runspec-linux ', ''], retention: { compact_after: ' 14D ', delete_after: '' } })
    expect(p).toEqual({
      targets: t, upgrade_runspec: true, packages: ['runspec-linux'], repo: undefined, inbox: true,
      retention: { compact_after: '14d' }, scheduler: undefined, become_method: 'sudo',
    })
    expect(hasWork(p)).toBe(true)
    const none = buildUpdatePayload(t, { ...FORM, upgradeRunspec: false, inbox: false, retention: null })
    expect(none.retention).toBeUndefined()
    expect(hasWork(none)).toBe(false)
    expect(hasWork({ ...none, scheduler: 'off' })).toBe(true)
  })

  it('notes what the options mean for each venv', () => {
    expect(updateNotes(row(), FORM)).toEqual([])
    expect(updateNotes(row(), { ...FORM, upgradeRunspec: false })[0]).toMatch(/0.79.0 here is too old for log cleanup/)
    expect(updateNotes(row({ runspec_version: '0.80.1' }), { ...FORM, upgradeRunspec: false })).toEqual([])
    expect(updateNotes(row(), { ...FORM, scheduler: 'off' })).toEqual(['No runspec-scheduler in this venv, so there is no daemon to stop.'])
    expect(updateNotes(row({ scheduler: true, backend: 'cron' }), { ...FORM, scheduler: 'auto' })).toContain("Its schedules move off the owner's crontab and the daemon fires them instead.")
  })

  it('counts what a schedules move takes', () => {
    const counts = (over: Partial<VenvScheduleCounts> = {}): VenvScheduleCounts =>
      ({ total: 5, disabled: 1, cron: 3, every: 1, in_crontab: 4, leftover: 0, ...over })
    const cron = row({ scheduler: true, backend: 'cron', schedules: counts() })
    const daemon = row({ host: 'web-02', scheduler: true, backend: 'daemon', schedules: counts({ in_crontab: 0, leftover: 2 }) })
    expect(moveText(cron, '')).toBe('')
    expect(moveText(cron, 'auto')).toBe('4 crontab entries move to the daemon (5 schedules in all; 1 disabled).')
    expect(moveText(row({ scheduler: true, backend: 'cron', schedules: counts({ in_crontab: 1, total: 1, disabled: 0 }) }), 'auto'))
      .toBe('1 crontab entry moves to the daemon (1 schedule in all).')
    expect(moveText(row({ scheduler: true, backend: 'cron', schedules: counts({ in_crontab: null }) }), 'cron')).toMatch(/how many is unknown/)
    expect(moveText(cron, 'off')).toBe('')
    expect(moveText(daemon, 'off')).toBe("3 schedules written to the owner's crontab (5 schedules in all; 1 disabled, 2 leftover crontab lines removed).")
    expect(moveText(daemon, 'auto')).toBe('Already on the daemon; 2 leftover crontab lines removed.')
    expect(moveTotal([cron, daemon, row({ host: 'x' })], 'auto')).toBe('Moves 4 crontab entries to the daemon on 1 host.')
    expect(moveTotal([cron, daemon], 'off')).toBe('Writes 3 schedules to crontabs on 1 host.')
    expect(moveTotal([cron], '')).toBe('')
    // Back to crontab drops interval schedules; a venv that couldn't be counted says so.
    expect(updateNotes(daemon, { ...FORM, scheduler: 'off' })[0]).toMatch(/^1 interval schedule can't run from cron/)
    expect(updateNotes(row({ scheduler: true, backend: 'cron', schedules: null, schedules_error: 'sudo: a password is required' }), { ...FORM, scheduler: 'auto' }))
      .toEqual(["Couldn't count this venv's schedules: sudo: a password is required.", "Its schedules move off the owner's crontab and the daemon fires them instead."])
  })

  it('offers leave / daemon keepers / back to crontab', () => {
    expect(UPDATE_SCHEDULER_OPTIONS.map(o => o.value)).toEqual(['', 'auto', 'systemd-user', 'supervisor', 'cron', 'off'])
    expect(UPDATE_SCHEDULER_OPTIONS[1].label).toBe('Move to the daemon, keeper picked per host')
  })
})

describe('UpdateVenvModal', () => {
  beforeEach(() => {
    api.provision_options.mockReset().mockResolvedValue(OPTS)
    api.venv_update_preview.mockReset().mockImplementation(async ({ targets }: { targets: { host: string }[] }) => ({
      rows: targets.map(t => t.host === 'web-02'
        ? row({ host: 'web-02', ok: false, blockers: ['Not a runspec venv (no pyvenv.cfg + bin/runspec).'] })
        : row({ host: t.host, inbox: true, retention: { compact_after: '3d' }, scheduler: true, backend: 'daemon', scheduler_pid: 77 })),
    }))
    api.venv_update_start.mockReset().mockResolvedValue({ op: 'op1', hosts: ['web-01'] })
  })

  it('previews the venv state and sends only ready hosts with the chosen options', async () => {
    const user = userEvent.setup()
    render(<UpdateVenvModal open initialTargets={[{ host: 'web-01', runspec_path: RP }]} onClose={() => {}} />)
    await waitFor(() => expect(api.venv_update_preview).toHaveBeenCalledWith({ targets: [{ host: 'web-01', runspec_path: RP }] }))
    expect(await screen.findByText(/runspec 0.79.0 · owner svc · inbox set up · log cleanup compress 3d · schedules daemon \(pid 77\)/)).toBeTruthy()

    // The Web group adds web-02 (connected), not db-01; web-02 isn't a venv.
    await user.click(screen.getByText('+ Group: Web'))
    expect(await screen.findByText(/Not a runspec venv/)).toBeTruthy()

    await user.click(screen.getByRole('checkbox', { name: /Set up the logs inbox/ }))
    await user.click(screen.getByRole('button', { name: /Update 1 host/ }))
    expect(api.venv_update_start).toHaveBeenCalledWith({
      targets: [{ host: 'web-01', runspec_path: RP }],
      upgrade_runspec: true,
      packages: [],
      repo: undefined,
      inbox: false,
      retention: { compact_after: '7d', delete_after: '90d' },
      scheduler: undefined,
      become_method: 'sudo',
    })
  })

  it('needs at least one option ticked', async () => {
    const user = userEvent.setup()
    render(<UpdateVenvModal open initialTargets={[{ host: 'web-01', runspec_path: RP }]} onClose={() => {}} />)
    await screen.findByText(/schedules daemon/)
    await user.click(screen.getByRole('checkbox', { name: /Upgrade runspec/ }))
    await user.click(screen.getByRole('checkbox', { name: /Set up the logs inbox/ }))
    await user.click(screen.getByRole('checkbox', { name: /Set the log cleanup policy/ }))
    expect(await screen.findByText('Tick at least one thing to update.')).toBeTruthy()
    expect((screen.getByRole('button', { name: /Update 1 host/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('stacks above the host editor it is opened from', async () => {
    render(<UpdateVenvModal open onClose={() => {}} zIndex={1100} />)
    await screen.findByText('Update a runspec venv')
    const wrap = document.querySelector('.ant-modal-wrap') as HTMLElement
    expect(wrap.style.zIndex).toBe('1100')
  })
})

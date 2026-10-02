/**
 * The Deprovision dialog: venv choices come only from paths hosts already
 * list, the preview runs read-only, and Delete stays disabled until the venv
 * name is typed; Keep logs (default on) rides the request.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { DeprovisionModal, formatKb, venvChoices, venvRoot } from './DeprovisionModal'
import type { ProvisionOptions } from '../bridge'

const { api } = vi.hoisted(() => ({
  api: {
    provision_options: vi.fn(),
    deprovision_preview: vi.fn(),
    deprovision_start: vi.fn(),
  } as Record<string, ReturnType<typeof vi.fn>>,
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: (_t, prop: string) => api[prop] ?? vi.fn().mockResolvedValue(undefined) }),
}))

const RP = '/opt/venvs/ops/bin/runspec'
const OPTS: ProvisionOptions = {
  hosts: [
    { name: 'web-01', connected: true, group: 'Web', profiles: [], runspec_paths: [RP, 'runspec'] },
    { name: 'web-02', connected: true, group: 'Web', profiles: [], runspec_paths: [RP] },
  ],
  groups: ['Web'],
  profiles: [],
  repos: [],
  min_python: '3.10',
}

describe('deprovision helpers', () => {
  it('derives venv roots and choices (POSIX paths only)', () => {
    expect(venvRoot(RP)).toBe('/opt/venvs/ops')
    const choices = venvChoices(OPTS.hosts)
    expect(choices).toEqual([
      { root: '/opt/venvs/ops', hosts: [{ host: 'web-01', runspec_path: RP }, { host: 'web-02', runspec_path: RP }] },
    ])
  })

  it('formats sizes', () => {
    expect(formatKb(512)).toBe('512 KB')
    expect(formatKb(2048)).toBe('2.0 MB')
    expect(formatKb(3 * 1024 * 1024)).toBe('3.0 GB')
  })
})

describe('DeprovisionModal', () => {
  beforeEach(() => {
    api.provision_options.mockReset().mockResolvedValue(OPTS)
    api.deprovision_preview.mockReset().mockResolvedValue({
      names: ['ops'],
      rows: [{
        host: 'web-01', runspec_path: RP, root: '/opt/venvs/ops', ok: true, kind: 'venv', owner: 'svc', user: 'deploy',
        size_kb: 2048, logs_kb: 512, runnables: ['disk-usage'], references: ['schedules'], blockers: [],
      }],
    })
    api.deprovision_start.mockReset().mockResolvedValue({ op: 'op1', hosts: ['web-01'] })
  })

  it('requires the typed venv name before Delete', async () => {
    const user = userEvent.setup()
    render(<DeprovisionModal open initialTargets={[{ host: 'web-01', runspec_path: RP }]} onClose={() => {}} />)
    await waitFor(() => expect(api.deprovision_preview).toHaveBeenCalledWith({ targets: [{ host: 'web-01', runspec_path: RP }] }))
    expect(await screen.findByText(/may be used by your schedules/)).toBeTruthy()

    const del = () => screen.getByRole('button', { name: /Delete on 1 host/ }) as HTMLButtonElement
    expect(del().disabled).toBe(true)
    await user.type(screen.getByLabelText('Confirm venv name'), 'op')
    expect(del().disabled).toBe(true)
    await user.type(screen.getByLabelText('Confirm venv name'), 's')
    expect(del().disabled).toBe(false)

    await user.click(del())
    expect(api.deprovision_start).toHaveBeenCalledWith({
      targets: [{ host: 'web-01', runspec_path: RP }],
      confirm: 'ops',
      keep_logs: true,
    })
  })

  it('sends keep_logs false when unticked', async () => {
    const user = userEvent.setup()
    render(<DeprovisionModal open initialTargets={[{ host: 'web-01', runspec_path: RP }]} onClose={() => {}} />)
    await user.click(await screen.findByRole('checkbox', { name: /Keep logs/ }))
    expect(await screen.findByText(/and its logs will be deleted/)).toBeTruthy()
    await user.type(screen.getByLabelText('Confirm venv name'), 'ops')
    await user.click(screen.getByRole('button', { name: /Delete on 1 host/ }))
    expect(api.deprovision_start).toHaveBeenCalledWith(expect.objectContaining({ keep_logs: false }))
  })

  it('stacks above the host editor it is opened from', async () => {
    render(<DeprovisionModal open onClose={() => {}} zIndex={1100} />)
    await screen.findByText('Deprovision a runspec venv')
    const wrap = document.querySelector('.ant-modal-wrap') as HTMLElement
    expect(wrap.style.zIndex).toBe('1100')
  })
})

/**
 * The Provision venv dialog: pure helpers (dest join, group/profile members,
 * payload), group quick-pick, and the preflight → provision flow — Provision
 * stays disabled until a preflight reports ready hosts, and only those hosts
 * are sent.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { ProvisionModal, buildPayload, joinDest, membersOf } from './ProvisionModal'
import type { ProvisionOptions } from '../bridge'

const { api } = vi.hoisted(() => ({
  api: {
    provision_options: vi.fn(),
    provision_interpreters: vi.fn(),
    provision_preflight: vi.fn(),
    provision_start: vi.fn(),
  } as Record<string, ReturnType<typeof vi.fn>>,
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: (_t, prop: string) => api[prop] ?? vi.fn().mockResolvedValue(undefined) }),
}))

const OPTS: ProvisionOptions = {
  hosts: [
    { name: 'web-01', connected: true, group: 'Web', profiles: ['prod'], runspec_paths: [] },
    { name: 'web-02', connected: true, group: 'Web', profiles: [], runspec_paths: [] },
    { name: 'db-01', connected: false, group: 'Web', profiles: ['prod'], runspec_paths: [] },
  ],
  groups: ['Web'],
  profiles: ['prod'],
  repos: [],
  min_python: '3.10',
}

describe('provision helpers', () => {
  it('joins a parent and a name', () => {
    expect(joinDest('/opt/venvs/', 'ops')).toBe('/opt/venvs/ops')
    expect(joinDest('/opt/venvs', '/ops/')).toBe('/opt/venvs/ops')
    expect(joinDest('', 'ops')).toBe('')
    expect(joinDest('/opt', '')).toBe('')
  })

  it('picks only connected members of a group / profile', () => {
    expect(membersOf(OPTS.hosts, 'group', 'Web')).toEqual(['web-01', 'web-02'])
    expect(membersOf(OPTS.hosts, 'profile', 'prod')).toEqual(['web-01'])
  })

  it('builds the payload from the version mode', () => {
    const p = buildPayload({
      hosts: ['web-01'], parent: '/opt/venvs', name: 'ops', pyMode: 'version', pyPath: '/usr/bin/python3',
      pyVersion: ' 3.12 ', runAs: ' svc ', become: 'sudo', packages: ['runspec-linux', ' '], repo: '',
    })
    expect(p).toEqual({
      hosts: ['web-01'], dest: '/opt/venvs/ops', python: '3.12', run_as: 'svc',
      become_method: 'sudo', packages: ['runspec-linux'], repo: undefined,
    })
  })
})

describe('ProvisionModal', () => {
  beforeEach(() => {
    api.provision_options.mockReset().mockResolvedValue(OPTS)
    api.provision_interpreters.mockReset().mockResolvedValue({
      ok: true, host: 'web-01', pythons: [
        { path: '/usr/bin/python3.12', real: '/usr/bin/python3.12', version: '3.12.4', runspec_ok: true },
        { path: '/usr/bin/python3', real: '/usr/bin/python3.9', version: '3.9.18', runspec_ok: false },
      ],
    })
    api.provision_preflight.mockReset().mockResolvedValue({
      dest: '/opt/venvs/ops',
      rows: [
        { host: 'web-01', ok: true, python: '/usr/bin/python3.12', version: '3.12.4', user: 'svc', dest_state: 'absent', blockers: [], warnings: [] },
        { host: 'web-02', ok: false, python: '', version: '', user: '', dest_state: '', blockers: ['No Python 3.12 found on this host.'], warnings: [] },
      ],
    })
    api.provision_start.mockReset().mockResolvedValue({ op: 'op1', hosts: ['web-01'] })
  })

  it('pre-picks the given host and its best interpreter', async () => {
    render(<ProvisionModal open initialHosts={['web-01', 'db-01']} onClose={() => {}} />)
    await waitFor(() => expect(api.provision_interpreters).toHaveBeenCalledWith({ host: 'web-01', extra_paths: [] }))
    // db-01 isn't connected, so it isn't pre-ticked.
    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[]
    expect(boxes.filter(b => b.checked)).toHaveLength(1)
    expect(await screen.findByText('/usr/bin/python3.12')).toBeTruthy()
  })

  it('adds group members, preflights, then provisions only ready hosts', async () => {
    const user = userEvent.setup()
    render(<ProvisionModal open initialHosts={[]} onClose={() => {}} />)
    await user.click(await screen.findByText('+ Group: Web'))
    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[]
    expect(boxes.filter(b => b.checked)).toHaveLength(2)

    await user.click(screen.getByText('Same version on each host'))
    await user.type(screen.getByLabelText('Venv name'), 'ops')
    await user.type(screen.getByLabelText('Run as'), 'svc')

    const provisionBtn = () => screen.getByRole('button', { name: /^Provision/ })
    expect((provisionBtn() as HTMLButtonElement).disabled).toBe(true)

    await user.click(screen.getByRole('button', { name: 'Check hosts' }))
    expect(api.provision_preflight).toHaveBeenCalledWith(
      expect.objectContaining({ hosts: ['web-01', 'web-02'], dest: '/opt/venvs/ops', python: '3.12', run_as: 'svc' }),
    )
    expect(await screen.findByText('No Python 3.12 found on this host.')).toBeTruthy()

    await waitFor(() => expect((provisionBtn() as HTMLButtonElement).disabled).toBe(false))
    await user.click(provisionBtn())
    expect(api.provision_start).toHaveBeenCalledWith(expect.objectContaining({ hosts: ['web-01'] }))
  })
})

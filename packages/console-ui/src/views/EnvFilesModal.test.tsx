/**
 * Tests for the Env files modal (.runspec_env manager). The bridge is mocked:
 * a save sends only the changed keys plus the loaded hash, a masked secret is
 * fetched only on Reveal, a conflict reloads, and the across-venvs flow
 * previews before it applies.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { EnvFilesModal, draftChanges, draftFromView, looksSecret, validKey } from './EnvFilesModal'
import type { EnvFileView, Host } from '../bridge'

const { api } = vi.hoisted(() => ({
  api: {
    env_file_targets: vi.fn(),
    env_file_read: vi.fn(),
    env_file_reveal: vi.fn(),
    env_file_save: vi.fn(),
    env_file_bulk_preview: vi.fn(),
    env_file_bulk_apply: vi.fn(),
  } as Record<string, ReturnType<typeof vi.fn>>,
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: (_t, prop: string) => api[prop] ?? vi.fn() }),
}))

const HOSTS: Host[] = [{ name: 'web-01', connected: true, runnableCount: 1, groups: ['ops'], group: 'Web', profiles: ['prod'] }]
const TARGETS = [
  { host: 'local', runspec_path: '/l/console/bin/runspec', venv: 'console', root: '/l/console', path: '/l/console/.runspec_env', group: '', profiles: [], local: true, connected: true, supported: true, reason: '' },
  { host: 'web-01', runspec_path: '/opt/venvs/ops/bin/runspec', venv: 'ops', root: '/opt/venvs/ops', path: '/opt/venvs/ops/.runspec_env', group: 'Web', profiles: ['prod'], local: false, connected: true, supported: true, reason: '' },
]
const VIEW: EnvFileView = {
  host: 'local', runspec_path: '/l/console/bin/runspec', venv: 'console', path: '/l/console/.runspec_env',
  ok: true, exists: true, owner: 'me', user: 'me', as_user: '', mode: '0600', sha: 'abc', writable: true,
  warnings: [], overrides: [],
  entries: [
    { key: 'JIRA_URL', value: 'https://jira', secret: false, masked: false, line: 1, duplicates: 0, readers: [] },
    { key: 'JIRA_PAT', value: '', secret: true, masked: true, line: 2, duplicates: 0,
      readers: [{ runnable: 'jira', command: '', arg: 'jira-pat', via: 'alias', password: true, description: '' }] },
  ],
  suggestions: [{ key: 'RUNSPEC_JIRA_ARG_ENV', secret: false, readers: [] }],
}

describe('draft helpers', () => {
  it('sends only edited, new and removed keys; masked values never round-trip', () => {
    const rows = draftFromView(VIEW)
    expect(draftChanges(rows)).toEqual({ set: {}, unset: [] })
    rows[0] = { ...rows[0], value: 'https://new', edited: true }
    rows[1] = { ...rows[1], removed: true }
    rows.push({ key: 'NEW', value: 'x', secret: false, masked: false, original: null, isNew: true, edited: true, removed: false, readers: [] })
    expect(draftChanges(rows)).toEqual({ set: { JIRA_URL: 'https://new', NEW: 'x' }, unset: ['JIRA_PAT'] })
  })

  it('validates names and spots secrets', () => {
    expect(validKey('A_1')).toBe(true)
    expect(validKey('1A')).toBe(false)
    expect(looksSecret('GITLAB_TOKEN')).toBe(true)
    expect(looksSecret('JIRA_URL')).toBe(false)
  })
})

describe('EnvFilesModal', () => {
  beforeEach(() => {
    Object.values(api).forEach(f => f.mockReset())
    api.env_file_targets.mockResolvedValue(TARGETS)
    api.env_file_read.mockResolvedValue(VIEW)
    api.env_file_reveal.mockResolvedValue({ key: 'JIRA_PAT', value: 'hunter2', found: true })
    api.env_file_save.mockResolvedValue({ ...VIEW, saved: true, changes: { added: [], changed: ['JIRA_URL'], removed: [] } })
  })

  it('loads the first venv, masks secrets, reveals on demand and saves changed keys with the hash', async () => {
    render(<EnvFilesModal open onClose={() => {}} hosts={HOSTS} />)
    expect(api.env_file_targets).toHaveBeenCalledWith(['web-01'])
    await screen.findByText('/l/console/.runspec_env')
    expect(api.env_file_read).toHaveBeenCalledWith({ host: 'local', runspec_path: '/l/console/bin/runspec' })
    expect(screen.getByText('•••••••• (hidden)')).toBeTruthy()
    expect(screen.getByText('jira --jira-pat')).toBeTruthy()

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Reveal JIRA_PAT' })) })
    expect(api.env_file_reveal).toHaveBeenCalledWith({ host: 'local', runspec_path: '/l/console/bin/runspec', key: 'JIRA_PAT' })
    await waitFor(() => expect(screen.queryByText('•••••••• (hidden)')).toBeNull())

    fireEvent.change(screen.getByLabelText('Value of JIRA_URL'), { target: { value: 'https://new' } })
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByRole('button', { name: /Save \(1\)/ })) })
    await act(async () => { await user.click(await screen.findByRole('button', { name: 'Save' })) })
    await waitFor(() => expect(api.env_file_save).toHaveBeenCalledTimes(1))
    expect(api.env_file_save.mock.calls[0][0]).toEqual({
      host: 'local', runspec_path: '/l/console/bin/runspec', sha: 'abc', set: { JIRA_URL: 'https://new' }, unset: [],
    })
  })

  it('reloads after a conflict', async () => {
    api.env_file_save.mockResolvedValue({ ...VIEW, ok: false, conflict: true, error: 'changed since you opened it' })
    render(<EnvFilesModal open onClose={() => {}} hosts={HOSTS} />)
    await screen.findByText('/l/console/.runspec_env')
    fireEvent.click(screen.getByRole('button', { name: 'Remove JIRA_URL' }))
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByRole('button', { name: /Save \(1\)/ })) })
    await act(async () => { await user.click(await screen.findByRole('button', { name: 'Save' })) })
    await waitFor(() => expect(api.env_file_read).toHaveBeenCalledTimes(2))
    expect(api.env_file_save.mock.calls[0][0].unset).toEqual(['JIRA_URL'])
  })

  it('across venvs: previews, then applies the one key to the picked venvs', async () => {
    api.env_file_bulk_preview.mockResolvedValue({ key: 'REGION', action: 'set', rows: [
      { host: 'web-01', runspec_path: '/opt/venvs/ops/bin/runspec', venv: 'ops', path: '', ok: true, state: 'absent', as_user: 'svc-ops' },
    ] })
    api.env_file_bulk_apply.mockResolvedValue({ key: 'REGION', action: 'set', ok: 1, changed: 1, failed: 0, rows: [
      { host: 'web-01', runspec_path: '/opt/venvs/ops/bin/runspec', venv: 'ops', path: '', ok: true, changed: true },
    ] })
    render(<EnvFilesModal open onClose={() => {}} hosts={HOSTS} />)
    await screen.findByText('/l/console/.runspec_env')
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByText('Across venvs')) })
    fireEvent.change(screen.getByLabelText('Variable name'), { target: { value: 'REGION' } })
    fireEvent.change(screen.getByLabelText('Value'), { target: { value: 'eu' } })
    // Pick by group.
    await act(async () => {
      await user.click(screen.getByRole('combobox', { name: 'Add a group' }))
      await user.click(await screen.findByTitle('Web'))
    })
    const apply = screen.getByRole('button', { name: 'Apply' })
    expect(apply.hasAttribute('disabled')).toBe(true)
    await act(async () => { await user.click(screen.getByRole('button', { name: 'Preview' })) })
    await screen.findByText('not set')
    expect(api.env_file_bulk_preview.mock.calls[0][0]).toEqual({
      key: 'REGION', action: 'set', value: 'eu', targets: [{ host: 'web-01', runspec_path: '/opt/venvs/ops/bin/runspec' }],
    })
    await act(async () => { await user.click(screen.getByRole('button', { name: 'Apply' })) })
    await screen.findByText(/Set REGION in 1 venv\?/)
    await act(async () => { await user.click(screen.getAllByRole('button', { name: 'Apply' }).slice(-1)[0]) })
    await waitFor(() => expect(api.env_file_bulk_apply).toHaveBeenCalledTimes(1))
    await screen.findByText('changed')
  })
})

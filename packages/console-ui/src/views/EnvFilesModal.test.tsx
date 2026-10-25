/**
 * Tests for the Env files modal (.runspec_env manager). The bridge is mocked:
 * a save sends only the changed keys plus the loaded hash, a masked secret is
 * fetched only on Reveal, a conflict reloads, and the across-venvs grid shows
 * every venv at once and saves each venv only its own changes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import {
  EnvFilesModal, cellToken, draftChanges, draftFromView, gridCell, gridChanges, looksSecret, rowSummary, validKey, valueGroups, venvColumns,
  venvSignature, withEdit, type GridDraft,
} from './EnvFilesModal'
import type { EnvFileView, Host } from '../bridge'

const { api } = vi.hoisted(() => ({
  api: {
    env_file_targets: vi.fn(),
    env_file_read: vi.fn(),
    env_file_reveal: vi.fn(),
    env_file_save: vi.fn(),
    env_file_read_many: vi.fn(),
    env_file_save_many: vi.fn(),
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

  it('across venvs: shows every venv in one grid, edits cells and saves each venv its own changes', { timeout: 30000 }, async () => {
    const web: EnvFileView = {
      ...VIEW, host: 'web-01', runspec_path: '/opt/venvs/ops/bin/runspec', venv: 'ops', path: '/opt/venvs/ops/.runspec_env', sha: 'w1',
      entries: [
        { key: 'JIRA_URL', value: 'https://jira', secret: false, masked: false, line: 1, duplicates: 0, readers: [] },
        { key: 'JIRA_PAT', value: '', secret: true, masked: true, fp: 'bbb', line: 2, duplicates: 0, readers: [] },
        { key: 'REGION', value: 'us', secret: false, masked: false, line: 3, duplicates: 0, readers: [] },
      ],
    }
    const local: EnvFileView = { ...VIEW, entries: VIEW.entries!.map(e => (e.masked ? { ...e, fp: 'aaa' } : e)) }
    api.env_file_read_many.mockResolvedValue({ views: [local, web] })
    api.env_file_save_many.mockResolvedValue({ ok: 2, saved: 2, failed: 0, rows: [
      { host: 'local', runspec_path: '/l/console/bin/runspec', venv: 'console', path: '', ok: true, saved: true },
      { host: 'web-01', runspec_path: '/opt/venvs/ops/bin/runspec', venv: 'ops', path: '', ok: true, saved: true },
    ] })
    render(<EnvFilesModal open onClose={() => {}} hosts={HOSTS} />)
    await screen.findByText('/l/console/.runspec_env')
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByText('Across venvs')) })
    await screen.findByText('REGION')
    expect(api.env_file_read_many).toHaveBeenCalledWith({ targets: [
      { host: 'local', runspec_path: '/l/console/bin/runspec' },
      { host: 'web-01', runspec_path: '/opt/venvs/ops/bin/runspec' },
    ] })
    // Every venv's values at once; the two hidden PATs differ (A / B), REGION is only on web-01.
    expect(screen.getAllByText('https://jira')).toHaveLength(2)
    expect(screen.getByText('2 values')).toBeTruthy()
    expect(screen.getByText('A')).toBeTruthy()
    expect(screen.getByText('B')).toBeTruthy()
    expect(screen.getAllByText('set in 1 of 2').length).toBeGreaterThan(0)

    // Only differences hides JIRA_URL (same everywhere).
    await act(async () => { await user.click(screen.getByRole('switch', { name: 'Only differences' })) })
    expect(screen.queryByText('JIRA_URL')).toBeNull()
    await act(async () => { await user.click(screen.getByRole('switch', { name: 'Only differences' })) })

    // Edit one cell in place.
    await act(async () => { await user.click(screen.getByRole('button', { name: 'Edit JIRA_URL on web-01 · ops' })) })
    const input = screen.getByLabelText('Value of JIRA_URL on web-01 · ops')
    await act(async () => { await user.clear(input); await user.type(input, 'https://new{Enter}') })

    // Set REGION everywhere: local gains it, web-01 changes.
    await act(async () => { await user.click(screen.getByRole('button', { name: 'Set REGION everywhere' })) })
    await act(async () => { await user.type(screen.getByLabelText('Value of REGION for every venv'), 'eu{Enter}') })

    await act(async () => { await user.click(screen.getByRole('button', { name: /Save \(3\)/ })) })
    await screen.findByText(/Save 3 changes in 2 venvs\?/)
    await act(async () => { await user.click(screen.getAllByRole('button', { name: 'Save' }).slice(-1)[0]) })
    await waitFor(() => expect(api.env_file_save_many).toHaveBeenCalledTimes(1))
    expect(api.env_file_save_many.mock.calls[0][0]).toEqual({ changes: [
      { host: 'web-01', runspec_path: '/opt/venvs/ops/bin/runspec', sha: 'w1', set: { JIRA_URL: 'https://new', REGION: 'eu' }, unset: [] },
      { host: 'local', runspec_path: '/l/console/bin/runspec', sha: 'abc', set: { REGION: 'eu' }, unset: [] },
    ] })
  })
  it('across venvs: identical venvs share a column, an edit there saves to each, and a variable lists its values', { timeout: 30000 }, async () => {
    const ops = (host: string, region: string, sha: string): EnvFileView => ({
      ...VIEW, host, runspec_path: '/opt/venvs/ops/bin/runspec', venv: 'ops', path: '/opt/venvs/ops/.runspec_env', sha,
      entries: [
        { key: 'JIRA_URL', value: 'https://jira.example.com/a/very/long/path/that/would/once/have/been/cut/off', secret: false, masked: false, line: 1, duplicates: 0, readers: [] },
        { key: 'REGION', value: region, secret: false, masked: false, line: 2, duplicates: 0, readers: [] },
      ],
    })
    const opsTarget = (host: string) => ({ ...TARGETS[1], host })
    api.env_file_targets.mockResolvedValue([TARGETS[0], opsTarget('web-01'), opsTarget('web-02'), opsTarget('web-03')])
    api.env_file_read_many.mockResolvedValue({ views: [VIEW, ops('web-01', 'eu', 's1'), ops('web-02', 'eu', 's2'), ops('web-03', 'us', 's3')] })
    api.env_file_save_many.mockResolvedValue({ ok: 2, saved: 2, failed: 0, rows: [] })
    render(<EnvFilesModal open onClose={() => {}} hosts={HOSTS} />)
    await screen.findByText('/l/console/.runspec_env')
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByText('Across venvs')) })
    await screen.findByText('REGION')
    // web-01 and web-02 hold the same file: one column for both.
    expect(screen.getByText('4 venvs in 3 columns')).toBeTruthy()
    expect(screen.getAllByText('2 hosts').length).toBeGreaterThan(0)
    // Values wrap rather than being cut off.
    expect(screen.getAllByText('https://jira.example.com/a/very/long/path/that/would/once/have/been/cut/off')).toHaveLength(2)

    // Click REGION: each value with the venvs holding it.
    await act(async () => { await user.click(screen.getByRole('button', { name: 'Show REGION across venvs' })) })
    const dialog = (await screen.findByText(/across 4 venvs/)).closest('.ant-modal') as HTMLElement
    expect(within(dialog).getByText('2 of 4 venvs')).toBeTruthy()
    expect(within(dialog).getByText('web-02 · ops')).toBeTruthy()
    expect(within(dialog).getByText('not set')).toBeTruthy()
    await act(async () => { await user.click(within(dialog).getByText('Close')) })

    // An edit in the shared column goes to both venvs.
    await act(async () => { await user.click(screen.getByRole('button', { name: 'Edit REGION on ops on 2 hosts' })) })
    const input = screen.getByLabelText('Value of REGION on ops on 2 hosts')
    await act(async () => { await user.clear(input); await user.type(input, 'ap{Enter}') })
    await act(async () => { await user.click(screen.getByRole('button', { name: /Save \(2\)/ })) })
    await screen.findByText(/Save 2 changes in 2 venvs\?/)
    await act(async () => { await user.click(screen.getAllByRole('button', { name: 'Save' }).slice(-1)[0]) })
    await waitFor(() => expect(api.env_file_save_many).toHaveBeenCalledTimes(1))
    expect(api.env_file_save_many.mock.calls[0][0]).toEqual({ changes: [
      { host: 'web-01', runspec_path: '/opt/venvs/ops/bin/runspec', sha: 's1', set: { REGION: 'ap' }, unset: [] },
      { host: 'web-02', runspec_path: '/opt/venvs/ops/bin/runspec', sha: 's2', set: { REGION: 'ap' }, unset: [] },
    ] })
  })
})

describe('grid helpers', () => {
  const view = (entries: Partial<EnvFileView['entries'] & object>): EnvFileView => ({ ...VIEW, entries: entries as EnvFileView['entries'] })
  const e = (key: string, value: string, extra = {}) => ({ key, value, secret: false, masked: false, line: 1, duplicates: 0, readers: [], ...extra })

  it('compares secrets by fingerprint and typed values by text', () => {
    const a = gridCell(view([e('T', '', { masked: true, fp: 'x' })]), 'T', undefined)
    const b = gridCell(view([e('T', '', { masked: true, fp: 'x' })]), 'T', undefined)
    const c = gridCell(view([]), 'T', undefined)
    expect(rowSummary([a, b, c])).toEqual({ setIn: 2, total: 3, distinct: 1, common: 'fp:x' })
    expect(cellToken(gridCell(view([]), 'T', 'typed'))).toBe('v:typed')
    expect(cellToken(gridCell(view([e('T', 'v')]), 'T', null))).toBeNull()
  })

  it('drops edits that put a cell back, and builds one change per venv', () => {
    const v = view([e('A', '1'), e('B', '2')])
    let d: GridDraft = {}
    d = withEdit(d, v, 'r', 'A', '1')         // same value: no edit
    expect(d).toEqual({})
    d = withEdit(d, v, 'r', 'A', '9')
    d = withEdit(d, v, 'r', 'B', null)
    d = withEdit(d, v, 'r', 'C', null)        // removing an absent key: no edit
    expect(d).toEqual({ r: { A: '9', B: null } })
    expect(gridChanges(d, { r: v })).toEqual([{ host: 'local', runspec_path: '/l/console/bin/runspec', sha: 'abc', set: { A: '9' }, unset: ['B'] }])
    d = withEdit(withEdit(d, v, 'r', 'A', undefined), v, 'r', 'B', undefined)
    expect(d).toEqual({})
  })
  it('merges venvs with the same file, edits and writability into one column', () => {
    const a = view([e('A', '1'), e('T', '', { masked: true, fp: 'x' })])
    const b = view([e('T', '', { masked: true, fp: 'x' }), e('A', '1')])
    const c = view([e('A', '1'), e('T', '', { masked: true, fp: 'y' })])
    const ro = { ...a, writable: false }
    const bad = { ...a, ok: false }
    expect(venvSignature(a, undefined)).toBe(venvSignature(b, {}))
    expect(venvSignature(a, undefined)).not.toBe(venvSignature(c, undefined))
    expect(venvSignature(a, undefined)).not.toBe(venvSignature(ro, undefined))
    expect(venvSignature(a, undefined)).not.toBe(venvSignature(a, { A: '2' }))
    expect(venvSignature(bad, undefined)).toBeNull()
    const views = { a, b, c, ro, bad, bad2: bad }
    expect(venvColumns(['a', 'c', 'b', 'ro', 'bad', 'bad2'], views, {}, true)).toEqual([
      { id: 'a', refs: ['a', 'b'] }, { id: 'c', refs: ['c'] }, { id: 'ro', refs: ['ro'] }, { id: 'bad', refs: ['bad'] }, { id: 'bad2', refs: ['bad2'] },
    ])
    // A pending edit on one splits it off; switching merging off gives a column each.
    expect(venvColumns(['a', 'b'], views, { b: { A: '2' } }, true)).toHaveLength(2)
    expect(venvColumns(['a', 'b'], views, {}, false)).toHaveLength(2)
  })

  it('never merges venvs with different names, even with the same file', () => {
    const f = view([e('A', '1')])
    const views = { local: f, h1: f, h2: f, u1: f, u2: f }
    const name: Record<string, string> = { local: 'console', h1: 'ops-tools', h2: 'ops-tools', u1: 'linux-utils', u2: 'linux-utils' }
    expect(venvColumns(['local', 'h1', 'u1', 'h2', 'u2'], views, {}, true, r => name[r])).toEqual([
      { id: 'local', refs: ['local'] }, { id: 'h1', refs: ['h1', 'h2'] }, { id: 'u1', refs: ['u1', 'u2'] },
    ])
  })

  it('groups a variable by value, most common first and unset last', () => {
    const views = { a: view([e('R', 'eu')]), b: view([e('R', 'us')]), c: view([]), d: view([e('R', 'us')]) }
    expect(valueGroups(['a', 'b', 'c', 'd'], views, {}, 'R')).toEqual([
      { token: 'v:us', refs: ['b', 'd'] }, { token: 'v:eu', refs: ['a'] }, { token: null, refs: ['c'] },
    ])
    expect(valueGroups(['a', 'c'], views, { c: { R: 'eu' } }, 'R')).toEqual([{ token: 'v:eu', refs: ['a', 'c'] }])
  })
})

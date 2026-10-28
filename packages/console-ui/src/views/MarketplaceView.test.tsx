/**
 * MarketplaceView: renders the catalogue as cards with per-venv install state,
 * filters by scope/repo/search, opens the install picker pre-selecting the
 * outdated venvs (run-as pre-filled from the venv owner), and starts an install
 * through bridge.marketplace_install with the chosen targets.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { MarketplaceView, cmpVersion, targetStates, filterEntries, defaultRunAs, opLabel, opOutcome, applyProgress, parseSearch, targetFilter, countUpdates } from './MarketplaceView'
import type { MarketplaceEntry, MarketplaceInstalled, MarketplaceInstalledTarget, MarketplaceOpRecord } from '../bridge'

const { catalog, installed, install, uninstall, cancel, opRec, opsList } = vi.hoisted(() => ({
  opRec: vi.fn(),
  opsList: vi.fn(),
  catalog: vi.fn(),
  installed: vi.fn(),
  install: vi.fn(),
  uninstall: vi.fn(),
  cancel: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'marketplace_catalog') return catalog
      if (p === 'marketplace_installed') return installed
      if (p === 'marketplace_install') return install
      if (p === 'marketplace_uninstall') return uninstall
      if (p === 'marketplace_cancel') return cancel
      if (p === 'marketplace_op') return opRec
      if (p === 'marketplace_ops') return opsList
      return vi.fn()
    },
  }),
}))

const entry = (over: Partial<MarketplaceEntry>): MarketplaceEntry => ({
  name: 'runspec-linux', display_name: 'runspec-linux', version: '0.13.2', versions: ['0.13.2', '0.13.1'],
  summary: 'Linux system admin runnables', description: '', description_truncated: false, description_content_type: '',
  keywords: ['linux'], links: { documentation: 'https://docs' }, requires_python: '>=3.10', requires_dist: ['runspec>=0.5'],
  license: '', author: '', upload_time: '', has_runspec_toml: false, metadata_source: 'pypi-json',
  repo: 'pypi', repo_name: 'PyPI', repo_url: 'https://pypi.org/simple/', repos: ['pypi'],
  ...over,
})

const target = (over: Partial<MarketplaceInstalledTarget>): MarketplaceInstalledTarget => ({
  id: 'web-01::/opt/venvs/ops', host: 'web-01', venv: 'ops', path: '/opt/venvs/ops', python: '/opt/venvs/ops/bin/python',
  runspec_path: '/opt/venvs/ops/bin/runspec', local: false, connected: true, ok: true, error: null,
  owner: 'svc', user: 'ops', python_version: '3.12.1', dists: {},
  ...over,
})

const RUNNING_OP: MarketplaceOpRecord = {
  op: 'op1', action: 'install', package: 'runspec-linux', status: 'running', results: [], started_at: Date.now() / 1000,
  options: { upgrade: true }, counts: { ok: 0, failed: 0, running: 0, queued: 1, skipped: 0 },
  targets: [{ target: 'web-01::/opt/venvs/ops', host: 'web-01', venv: 'ops', state: 'queued', lines: [] }],
}

const ENTRIES = [
  entry({}),
  entry({ name: 'runspec-jira', display_name: 'runspec-jira', version: '0.3.0', versions: ['0.3.0'], summary: 'Jira runnable', keywords: ['jira'], links: {} }),
  entry({ name: 'acme-ops', display_name: 'acme-ops', version: '2.0', versions: ['2.0'], summary: 'Acme internal', keywords: [], repo: 'nexus', repo_name: 'Nexus', repos: ['nexus'], has_runspec_toml: true }),
]

const INSTALLED: MarketplaceInstalled = {
  probed_at: Date.now() / 1000,
  targets: [
    target({ id: 'local::/venvs/console', host: 'local', venv: 'console', path: '/venvs/console', local: true, owner: '', user: 'me', dists: { runspec: '0.5', 'runspec-jira': '0.3.0' } }),
    target({ dists: { runspec: '0.5', 'runspec-linux': '0.13.1' } }),
    target({ id: 'db-01::/opt/venvs/db', host: 'db-01', venv: 'db', connected: false, ok: false, error: 'host not connected', owner: '', user: '', python_version: '' }),
  ],
}

const HOSTS = [
  { name: 'local', connected: true, runnableCount: 1, groups: ['console'] },
  { name: 'web-01', connected: true, runnableCount: 1, groups: ['ops'], group: 'Production' },
  { name: 'db-01', connected: false, runnableCount: 0, groups: ['db'] },
]

describe('pure helpers', () => {
  it('compares versions PEP 440-ish', () => {
    expect(cmpVersion('0.13.1', '0.13.2')).toBe(-1)
    expect(cmpVersion('1.0', '1.0.0')).toBe(0)
    expect(cmpVersion('1.0rc1', '1.0')).toBe(-1)
    expect(cmpVersion('1.0.dev1', '1.0a1')).toBe(-1)
    expect(cmpVersion('2.0', '1.9.9')).toBe(1)
  })

  it('derives per-target install state', () => {
    const st = targetStates(ENTRIES[0], INSTALLED)
    expect(st.map(s => [s.target.host, s.installed, s.state])).toEqual([
      ['local', null, 'absent'], ['web-01', '0.13.1', 'outdated'], ['db-01', null, 'unknown'],
    ])
    expect(targetStates(ENTRIES[0], null)).toEqual([])
  })

  it('filters by scope, repo and search', () => {
    const states = (e: MarketplaceEntry) => targetStates(e, INSTALLED)
    const names = (o: Parameters<typeof filterEntries>[1]) => filterEntries(ENTRIES, o, states).map(e => e.name)
    expect(names({ search: '', repo: '', scope: 'all' })).toEqual(['runspec-linux', 'runspec-jira', 'acme-ops'])
    expect(names({ search: '', repo: '', scope: 'installed' })).toEqual(['runspec-linux', 'runspec-jira'])
    expect(names({ search: '', repo: '', scope: 'updates' })).toEqual(['runspec-linux'])
    expect(names({ search: '', repo: '', scope: 'available' })).toEqual(['acme-ops'])
    expect(names({ search: '', repo: 'nexus', scope: 'all' })).toEqual(['acme-ops'])
    expect(names({ search: 'jira', repo: '', scope: 'all' })).toEqual(['runspec-jira'])
    expect(names({ search: 'internal acme', repo: '', scope: 'all' })).toEqual(['acme-ops'])
  })

  it('splits host: / group: tokens out of the search', () => {
    expect(parseSearch('jira host:WEB group:prod linux')).toEqual({ text: 'jira linux', hosts: ['web'], groups: ['prod'] })
    expect(parseSearch('host:')).toEqual({ text: '', hosts: [], groups: [] })
    expect(parseSearch('  ')).toEqual({ text: '', hosts: [], groups: [] })
  })

  it('keeps the venvs on matching hosts / groups', () => {
    const groupOf = (h: string) => (h === 'web-01' ? 'Production' : undefined)
    const hosts = (q: string) => INSTALLED.targets.filter(targetFilter(parseSearch(q), groupOf)!).map(t => t.host)
    expect(targetFilter(parseSearch('jira'), groupOf)).toBeNull()
    expect(hosts('host:web')).toEqual(['web-01'])
    expect(hosts('host:web host:db')).toEqual(['web-01', 'db-01'])
    expect(hosts('group:prod')).toEqual(['web-01'])
    expect(hosts('group:prod host:db')).toEqual([])
  })

  it('counts updates on the venvs it is given', () => {
    const all = (e: MarketplaceEntry) => targetStates(e, INSTALLED)
    expect(countUpdates(ENTRIES, all)).toBe(1)
    const localOnly = (e: MarketplaceEntry) => all(e).filter(s => s.target.host === 'local')
    expect(countUpdates(ENTRIES, localOnly)).toBe(0)
  })

  it('pre-fills run-as from a remote venv owned by someone else', () => {
    expect(defaultRunAs(target({}))).toBe('svc')
    expect(defaultRunAs(target({ owner: 'ops' }))).toBe('')
    expect(defaultRunAs(target({ local: true }))).toBe('')
  })
})

describe('MarketplaceView', () => {
  beforeEach(() => {
    catalog.mockReset().mockResolvedValue({
      enabled: true, entries: ENTRIES, errors: ['PyPI: runspec-gtilab-core: no releases'],
      repos: [
        { id: 'pypi', name: 'PyPI', url: 'https://pypi.org/simple/', source: 'pip', scan: true, packages: [], count: 2, fetched_at: Date.now() / 1000 - 60, stale: false, error: null },
        { id: 'nexus', name: 'Nexus', url: 'https://nexus/simple/', source: 'config', scan: true, packages: [], count: 1, fetched_at: null, stale: false, error: null },
      ],
    })
    installed.mockReset().mockResolvedValue(INSTALLED)
    install.mockReset().mockResolvedValue({ op: 'op1', targets: [] })
    uninstall.mockReset().mockResolvedValue({ op: 'op2', targets: [] })
    cancel.mockReset().mockResolvedValue(true)
    opRec.mockReset().mockResolvedValue(RUNNING_OP)
    opsList.mockReset().mockResolvedValue([])
  })

  it('renders cards with install state, links and the skipped-count tag', async () => {
    render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('runspec-linux')
    await waitFor(() => expect(installed).toHaveBeenCalled())
    // outdated on web-01 → "update available" + the ↑ chip
    await screen.findByText('update available')
    expect(screen.getByText(/web-01\/ops 0\.13\.1 ↑/)).toBeTruthy()
    // installed-and-current elsewhere
    expect(screen.getByText('installed')).toBeTruthy()
    expect(screen.getByText(/local\/console 0\.3\.0/)).toBeTruthy()
    // not installed anywhere
    expect(screen.getByText('Not installed on any venv')).toBeTruthy()
    expect(screen.getByText('verified')).toBeTruthy()
    expect(screen.getAllByRole('link', { name: /Docs/ })[0].getAttribute('href')).toBe('https://docs')
    expect(screen.getByText('1 package skipped')).toBeTruthy()
    expect(screen.getByText(/PyPI · 2 · read/)).toBeTruthy()
  })

  it('filters with the search box', async () => {
    render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('runspec-linux')
    const user = userEvent.setup()
    await act(async () => { await user.type(screen.getByPlaceholderText(/Search packages/), 'acme') })
    await waitFor(() => expect(screen.queryByText('runspec-linux')).toBeNull())
    expect(screen.getByText('acme-ops')).toBeTruthy()
  })

  it('limits install state to the hosts named by host:', async () => {
    render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('update available')
    const user = userEvent.setup()
    await act(async () => { await user.type(screen.getByPlaceholderText(/Search packages/), 'host:local') })
    expect(await screen.findByText(/host:local · 1 venv/)).toBeTruthy()
    // web-01's outdated install is out of scope now; local's runspec-jira stays.
    await waitFor(() => expect(screen.queryByText('update available')).toBeNull())
    expect(screen.queryByText(/web-01\/ops/)).toBeNull()
    expect(screen.getByText(/local\/console 0\.3\.0/)).toBeTruthy()
    // Every package still lists (scope "All"); "Installed" narrows to local's.
    expect(screen.getByText('runspec-linux')).toBeTruthy()
    await act(async () => { await user.click(screen.getByText('Installed')) })
    await waitFor(() => expect(screen.queryByText('runspec-linux')).toBeNull())
    expect(screen.getByText('runspec-jira')).toBeTruthy()
  })

  it('filters by group: from the host list', async () => {
    render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('update available')
    const user = userEvent.setup()
    await act(async () => { await user.type(screen.getByPlaceholderText(/Search packages/), 'group:production') })
    expect(await screen.findByText(/group:production · 1 venv/)).toBeTruthy()
    expect(screen.getByText('update available')).toBeTruthy()
    expect(screen.queryByText(/local\/console/)).toBeNull()
  })

  it('shows the update count, jumps to Updates, and tells the tab badge', async () => {
    const seen: number[] = []
    const onCount = (e: Event) => seen.push((e as CustomEvent).detail.count)
    window.addEventListener('runspec:marketplace_updates', onCount)
    try {
      render(<MarketplaceView hosts={HOSTS} />)
      const tag = await screen.findByText(/1 update available/)
      await waitFor(() => expect(seen.at(-1)).toBe(1))
      const user = userEvent.setup()
      await act(async () => { await user.click(tag) })
      await waitFor(() => expect(screen.queryByText('runspec-jira')).toBeNull())
      expect(screen.getByText('runspec-linux')).toBeTruthy()
    } finally {
      window.removeEventListener('runspec:marketplace_updates', onCount)
    }
  })

  it('pre-selects only the filtered host in the install picker', async () => {
    render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('runspec-linux')
    await waitFor(() => expect(installed).toHaveBeenCalled())
    const user = userEvent.setup()
    await act(async () => { await user.type(screen.getByPlaceholderText(/Search packages/), 'host:local') })
    await screen.findByText(/host:local · 1 venv/)
    // acme-ops is installed nowhere: without a filter both venvs would be ticked.
    const card = screen.getByText('acme-ops').closest('.rs-card-lift') as HTMLElement
    await act(async () => { await user.click(within(card).getByRole('button', { name: /Install…/ })) })
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getAllByRole('checkbox').map(b => (b as HTMLInputElement).checked)).toEqual([true, false])
  })

  it('opens the update picker with the outdated venv pre-selected and starts the install', async () => {
    render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('update available')
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByRole('button', { name: /Update…/ })) })
    const dialog = await screen.findByRole('dialog')
    // Connected + reachable venvs only (db-01 is not connected).
    expect(within(dialog).queryByText('db-01')).toBeNull()
    const boxes = within(dialog).getAllByRole('checkbox')
    expect(boxes.map(b => (b as HTMLInputElement).checked)).toEqual([false, true])  // local absent, web-01 outdated
    // run-as pre-filled from the venv owner (svc ≠ ops).
    expect(within(dialog).getByDisplayValue('svc')).toBeTruthy()
    await act(async () => { await user.click(within(dialog).getByRole('button', { name: /Install runspec-linux on 1 venv$/ })) })
    await waitFor(() => expect(install).toHaveBeenCalled())
    expect(install.mock.calls[0][0]).toEqual({
      package: 'runspec-linux', upgrade: true, reinstall: false, repo: 'pypi', upgrade_runspec: false,
      targets: [{ id: 'web-01::/opt/venvs/ops', run_as: 'svc', become_method: 'sudo' }],
    })
    // The picker hands off to the op's live view (closable while it runs).
    const live = await screen.findByRole('dialog', { name: /Installing runspec-linux/ })
    expect(within(live).getByText(/Closing this keeps it running/)).toBeTruthy()
    // Streamed progress lands in the per-target log; done re-probes the venvs.
    await act(async () => {
      window.dispatchEvent(new CustomEvent('runspec:marketplace_progress', { detail: { op: 'op1', phase: 'start', target: 'web-01::/opt/venvs/ops' } }))
      window.dispatchEvent(new CustomEvent('runspec:marketplace_progress', { detail: { op: 'op1', phase: 'line', target: 'web-01::/opt/venvs/ops', line: 'Successfully installed runspec-linux-0.13.2' } }))
    })
    expect(await within(live).findByText(/Successfully installed runspec-linux-0\.13\.2/)).toBeTruthy()
    const before = installed.mock.calls.length
    opRec.mockResolvedValue({ ...RUNNING_OP, status: 'finished', counts: { ok: 1, failed: 0, running: 0, queued: 0, skipped: 0 },
      targets: [{ ...RUNNING_OP.targets![0], state: 'done', ok: true, exit_code: 0, summary: 'ok', lines: ['Successfully installed runspec-linux-0.13.2'] }] })
    await act(async () => {
      window.dispatchEvent(new CustomEvent('runspec:marketplace_done', { detail: { op: 'op1', action: 'install', status: 'finished', results: [{ target: 'web-01::/opt/venvs/ops', host: 'web-01', venv: 'ops', ok: true, exit_code: 0, summary: 'ok', skipped: false }] } }))
    })
    await waitFor(() => expect(installed.mock.calls.length).toBeGreaterThan(before))
    expect((await within(live).findAllByText('ok')).length).toBeGreaterThan(0)
  })

  it('sends the "also upgrade runspec" flag', async () => {
    render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('update available')
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByRole('button', { name: /Update…/ })) })
    const dialog = await screen.findByRole('dialog')
    await act(async () => { await user.click(within(dialog).getByRole('switch', { name: 'Also upgrade runspec' })) })
    await act(async () => { await user.click(within(dialog).getByRole('button', { name: /Install runspec-linux on 1 venv$/ })) })
    await waitFor(() => expect(install).toHaveBeenCalled())
    expect(install.mock.calls[0][0].upgrade_runspec).toBe(true)
  })

  it('lists running and past operations and reopens one from Activity', async () => {
    const done: MarketplaceOpRecord = { ...RUNNING_OP, op: 'op0', package: 'runspec-jira', status: 'finished', started_at: Date.now() / 1000 - 600,
      counts: { ok: 0, failed: 1, running: 0, queued: 0, skipped: 0 },
      targets: [{ ...RUNNING_OP.targets![0], state: 'done', ok: false, exit_code: 1, summary: 'ERROR: boom', lines: ['ERROR: boom'] }] }
    opsList.mockResolvedValue([RUNNING_OP, done])
    opRec.mockImplementation(async (op: string) => (op === 'op0' ? done : RUNNING_OP))
    render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('runspec-linux')
    // The running op shows in the strip and on its package card.
    expect(await screen.findByText(/Installing runspec-linux · 0\/1 done/)).toBeTruthy()
    expect(screen.getByText('installing…')).toBeTruthy()
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByRole('button', { name: /Activity/ })) })
    const list = await screen.findByRole('dialog', { name: 'Marketplace activity' })
    expect(within(list).getByText('1 of 1 failed')).toBeTruthy()
    await act(async () => { await user.click(within(list).getByText('Install runspec-jira')) })
    await waitFor(() => expect(opRec).toHaveBeenCalledWith('op0'))
    expect(await screen.findByText('ERROR: boom')).toBeTruthy()
    expect(screen.getByText('failed (exit 1)')).toBeTruthy()
  })

  it('uninstall lists only venvs that have the package', async () => {
    render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('update available')
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getAllByRole('button', { name: /Uninstall…/ })[0]) })
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getAllByRole('checkbox')).toHaveLength(1)
    await act(async () => { await user.click(within(dialog).getByRole('button', { name: /Uninstall runspec-linux on 1 venv/ })) })
    await waitFor(() => expect(uninstall).toHaveBeenCalled())
    expect(uninstall.mock.calls[0][0].targets).toEqual([{ id: 'web-01::/opt/venvs/ops', run_as: 'svc', become_method: 'sudo' }])
  })

  it('shows the active host-profile cue only when a profile is active', async () => {
    const { rerender } = render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('runspec-linux')
    await waitFor(() => expect(installed).toHaveBeenCalled())
    // "All hosts" (no profile) → no scoping cue.
    expect(screen.queryByText(/profile:/)).toBeNull()
    // A named profile → the cue names it (so a not-installed chip is understood
    // as "not in this profile", not "missing").
    rerender(<MarketplaceView hosts={HOSTS} activeProfile="ops" />)
    expect(await screen.findByText('profile: ops')).toBeTruthy()
  })

  it('re-probes the venvs when the window regains focus and the probe is stale', async () => {
    installed.mockReset().mockResolvedValue({ ...INSTALLED, probed_at: Date.now() / 1000 - 999 })
    render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('runspec-linux')
    await waitFor(() => expect(installed).toHaveBeenCalled())
    const before = installed.mock.calls.length
    await act(async () => { window.dispatchEvent(new Event('focus')) })
    await waitFor(() => expect(installed.mock.calls.length).toBeGreaterThan(before))
  })

  it('does not re-probe on focus when the probe is fresh', async () => {
    render(<MarketplaceView hosts={HOSTS} />)  // INSTALLED.probed_at is ~now
    await screen.findByText('runspec-linux')
    await waitFor(() => expect(installed).toHaveBeenCalled())
    const before = installed.mock.calls.length
    await act(async () => { window.dispatchEvent(new Event('focus')) })
    // Give any errant probe a tick to fire, then assert it didn't.
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(installed.mock.calls.length).toBe(before)
  })

  it('summarises + scroll-boxes the install chips when a package is on many venvs', async () => {
    const many = Array.from({ length: 8 }, (_, i) =>
      target({ id: `h${i}::/opt/v`, host: `h${i}`, venv: 'v', dists: { 'runspec-linux': i === 0 ? '0.13.1' : '0.13.2' } }),
    )
    installed.mockReset().mockResolvedValue({ probed_at: Date.now() / 1000, targets: many })
    render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('runspec-linux')
    await waitFor(() => expect(installed).toHaveBeenCalled())
    // A count summary (8 venvs, 1 of them outdated → "1 to update").
    expect(await screen.findByText(/Installed on 8 venvs · 1 to update/)).toBeTruthy()
    // The chips still render (one per venv) inside the scroll box.
    expect(screen.getByText(/h0\/v 0\.13\.1 ↑/)).toBeTruthy()
  })

  it('shows the off notice when the marketplace is disabled', async () => {
    catalog.mockResolvedValue({ enabled: false, entries: [], repos: [], errors: [] })
    render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('The marketplace is turned off')
  })
})

describe('op helpers', () => {
  it('labels, summarises and applies progress', () => {
    expect(opLabel(RUNNING_OP)).toBe('Installing runspec-linux')
    expect(opLabel({ ...RUNNING_OP, action: 'uninstall', status: 'finished' })).toBe('Uninstall runspec-linux')
    expect(opOutcome(RUNNING_OP)).toEqual({ color: 'processing', text: '0/1 done · 1 queued' })
    expect(opOutcome({ ...RUNNING_OP, status: 'cancelled' }).text).toBe('cancelled')
    expect(opOutcome({ ...RUNNING_OP, status: 'finished', counts: { ok: 1, failed: 0, running: 0, queued: 0, skipped: 0 } }).text).toBe('ok')
    let r = applyProgress(RUNNING_OP, { phase: 'start', target: 'web-01::/opt/venvs/ops' })
    expect(r.targets![0].state).toBe('running')
    r = applyProgress(r, { phase: 'line', target: 'web-01::/opt/venvs/ops', line: 'hi' })
    r = applyProgress(r, { phase: 'done', target: 'web-01::/opt/venvs/ops', ok: true, exit_code: 0, summary: 'ok' })
    expect(r.targets![0]).toMatchObject({ state: 'done', ok: true, lines: ['hi'] })
    expect(applyProgress(r, { phase: 'line', target: 'other', line: 'x' }).targets).toEqual(r.targets)
  })
})

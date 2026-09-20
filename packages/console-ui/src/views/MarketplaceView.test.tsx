/**
 * MarketplaceView: renders the catalogue as cards with per-venv install state,
 * filters by scope/repo/search, opens the install picker pre-selecting the
 * outdated venvs (run-as pre-filled from the venv owner), and starts an install
 * through bridge.marketplace_install with the chosen targets.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { MarketplaceView, cmpVersion, targetStates, filterEntries, defaultRunAs } from './MarketplaceView'
import type { MarketplaceEntry, MarketplaceInstalled, MarketplaceInstalledTarget } from '../bridge'

const { catalog, installed, install, uninstall, cancel } = vi.hoisted(() => ({
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
  { name: 'web-01', connected: true, runnableCount: 1, groups: ['ops'] },
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
    await act(async () => { await user.type(screen.getByPlaceholderText(/Search name/), 'acme') })
    await waitFor(() => expect(screen.queryByText('runspec-linux')).toBeNull())
    expect(screen.getByText('acme-ops')).toBeTruthy()
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
      package: 'runspec-linux', upgrade: true, reinstall: false, repo: 'pypi',
      targets: [{ id: 'web-01::/opt/venvs/ops', run_as: 'svc', become_method: 'sudo' }],
    })
    // Streamed progress lands in the per-target log; done re-probes the venvs.
    await act(async () => {
      window.dispatchEvent(new CustomEvent('runspec:marketplace_progress', { detail: { op: 'op1', phase: 'line', target: 'web-01::/opt/venvs/ops', line: 'Successfully installed runspec-linux-0.13.2' } }))
    })
    expect(await within(dialog).findByText(/Successfully installed runspec-linux-0\.13\.2/)).toBeTruthy()
    const before = installed.mock.calls.length
    await act(async () => {
      window.dispatchEvent(new CustomEvent('runspec:marketplace_done', { detail: { op: 'op1', action: 'install', status: 'finished', results: [{ target: 'web-01::/opt/venvs/ops', host: 'web-01', venv: 'ops', ok: true, exit_code: 0, summary: 'ok', skipped: false }] } }))
    })
    await waitFor(() => expect(installed.mock.calls.length).toBeGreaterThan(before))
    expect(await within(dialog).findByText('ok')).toBeTruthy()
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

  it('shows the off notice when the marketplace is disabled', async () => {
    catalog.mockResolvedValue({ enabled: false, entries: [], repos: [], errors: [] })
    render(<MarketplaceView hosts={HOSTS} />)
    await screen.findByText('The marketplace is turned off')
  })
})

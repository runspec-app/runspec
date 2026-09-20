/**
 * Settings → Marketplace: lists the pip-derived indexes read-only and the
 * declared repos with their controls, and saves a new declared repo through
 * save_marketplace_config (declared repos only — pip-derived rows never round-trip).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { MarketplaceTab } from './SettingsDrawer'

const { getCfg, saveCfg, clearCache } = vi.hoisted(() => ({
  getCfg: vi.fn(),
  saveCfg: vi.fn(),
  clearCache: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'get_marketplace_config') return getCfg
      if (p === 'save_marketplace_config') return saveCfg
      if (p === 'marketplace_clear_cache') return clearCache
      return vi.fn()
    },
  }),
}))

const NEXUS = { id: 'nexus', name: 'Nexus', url: 'https://nexus/simple/', enabled: true, scan: true, packages: ['acme-ops'], patterns: [], credential: 'svc', pip_arg: 'auto', trusted_host: '', tags: [], source: 'config', pip_role: 'index', is_pypi: false }
const PYPI = { id: 'pypi', name: 'PyPI', url: 'https://pypi.org/simple/', enabled: true, scan: true, packages: [], patterns: [], credential: '', pip_arg: 'auto', trusted_host: '', tags: ['pip-config'], source: 'pip', pip_role: 'extra', is_pypi: true }

const CFG = {
  enabled: true, auto_pip: true, repos: [NEXUS, PYPI], declared_repos: [NEXUS],
  patterns: ['runspec-*'], exclude: ['runspec'], require_runspec: true, include_prerelease: false,
  cache_ttl: 86400, timeout: 20, max_index_bytes: 1, max_wheel_bytes: 1, ca_bundle: null, tls_verify: true,
  pip_sources: ['/etc/pip.conf'], credential_options: [{ id: 'svc', label: 'Service', kind: 'userpass' }], cache_path: '/x',
}

describe('MarketplaceTab', () => {
  beforeEach(() => {
    getCfg.mockReset().mockResolvedValue(CFG)
    saveCfg.mockReset().mockImplementation(async (sec) => ({ ...CFG, declared_repos: sec.repos, repos: [...sec.repos, PYPI] }))
    clearCache.mockReset().mockResolvedValue(undefined)
  })

  it('lists pip-derived and declared repos', async () => {
    render(<MarketplaceTab />)
    await screen.findByText('Nexus')
    expect(screen.getByText('PyPI')).toBeTruthy()
    expect(screen.getByText('extra-index-url')).toBeTruthy()
    expect(screen.getByText('1 named')).toBeTruthy()
    expect(screen.getByText(/auth: Service/)).toBeTruthy()
    expect(screen.getByText(/\/etc\/pip\.conf/)).toBeTruthy()
  })

  it('adds a declared repo and saves only the declared list', async () => {
    render(<MarketplaceTab />)
    await screen.findByText('Nexus')
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByRole('button', { name: /Add repo/ })) })
    const dialog = await screen.findByRole('dialog')
    await act(async () => {
      await user.type(within(dialog).getByLabelText('Name'), 'Artifactory')
      await user.type(within(dialog).getByLabelText('Index URL'), 'https://af.corp/api/pypi/pypi/simple')
      await user.type(within(dialog).getByLabelText('Named packages'), 'acme-a, acme-b')
    })
    await act(async () => { await user.click(within(dialog).getByRole('button', { name: /Save/ })) })
    await waitFor(() => expect(saveCfg).toHaveBeenCalled())
    const sec = saveCfg.mock.calls[0][0]
    expect(sec.repos.map((r: { name: string }) => r.name)).toEqual(['Nexus', 'Artifactory'])
    expect(sec.repos[1]).toMatchObject({ url: 'https://af.corp/api/pypi/pypi/simple', packages: ['acme-a', 'acme-b'], scan: true, pip_arg: 'auto' })
    expect(sec.auto_pip).toBe(true)
  })

  it('toggling auto_pip persists it', async () => {
    render(<MarketplaceTab />)
    await screen.findByText('Nexus')
    const user = userEvent.setup()
    const switches = screen.getAllByRole('switch')
    await act(async () => { await user.click(switches[1]) })  // 2nd = auto_pip
    await waitFor(() => expect(saveCfg).toHaveBeenCalled())
    expect(saveCfg.mock.calls[0][0].auto_pip).toBe(false)
  })

  it('clears the catalogue cache', async () => {
    render(<MarketplaceTab />)
    await screen.findByText('Nexus')
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByRole('button', { name: /Clear catalogue cache/ })) })
    await waitFor(() => expect(clearCache).toHaveBeenCalled())
  })
})

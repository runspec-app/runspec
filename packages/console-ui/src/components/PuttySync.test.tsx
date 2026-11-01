/**
 * PuttySync: the [putty] config round-trip, the result summary, and the
 * Sync now / Remove buttons calling the bridge.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { PuttySync, puttySyncToConfig, summarizeSync } from './PuttySync'
import { quickConnectOptions } from './SettingsDrawer'

const { status, syncNow, getConfig, saveConfig } = vi.hoisted(() => ({
  status: vi.fn(),
  syncNow: vi.fn(),
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'putty_sync_status') return status
      if (p === 'putty_sync_now') return syncNow
      if (p === 'get_config') return getConfig
      if (p === 'save_config') return saveConfig
      if (p === 'get_profiles') return async () => [{ name: 'web', description: '' }]
      return vi.fn(async () => undefined)
    },
  }),
}))

const baseStatus = {
  supported: true, putty: 'C:/PuTTY/putty.exe', plink: 'C:/PuTTY/plink.exe',
  config: { sync: false, profiles: [], keepalive: 30, prefix: '' }, sessions: ['prod/web-01'], last: null,
}

beforeEach(() => {
  status.mockReset().mockResolvedValue(baseStatus)
  syncNow.mockReset()
  getConfig.mockReset().mockResolvedValue({ ssh: { user: 'ops' } })
  saveConfig.mockReset().mockResolvedValue(undefined)
})

describe('puttySyncToConfig', () => {
  it('drops empty profiles and prefix', () => {
    expect(puttySyncToConfig({ sync: true, profiles: [' ', 'web'], keepalive: 29.6, prefix: ' ' }))
      .toEqual({ sync: true, keepalive: 30, profiles: ['web'] })
  })
})

describe('summarizeSync', () => {
  it('counts written, removed and skipped', () => {
    expect(summarizeSync({
      ok: true, supported: true, written: [{ host: 'a', session: 'a' }], removed: ['x', 'y'],
      skipped: [{ host: 'b', reason: 'no key' }],
    })).toBe('1 session written, 2 removed, 1 skipped')
  })
  it('explains off Windows', () => {
    expect(summarizeSync({ ok: false, supported: false, written: [], removed: [], skipped: [] }))
      .toMatch(/only on Windows/)
  })
})

describe('PuttySync', () => {
  it('syncs now and lists skipped hosts', async () => {
    syncNow.mockResolvedValue({
      ok: true, supported: true, written: [{ host: 'web-01', session: 'prod/web-01' }], removed: [],
      skipped: [{ host: 'behind', reason: 'plink.exe not found' }],
    })
    render(<PuttySync />)
    await screen.findByText('1 saved by the console')
    await userEvent.click(screen.getByRole('button', { name: /Sync now/ }))
    const result = await screen.findByTestId('putty-sync-result')
    expect(result.textContent).toContain('1 session written, 1 skipped')
    expect(result.textContent).toContain('plink.exe not found')
  })

  it('saves [putty] without touching the rest of the config', async () => {
    render(<PuttySync />)
    await screen.findByText('1 saved by the console')
    await userEvent.click(screen.getByRole('switch', { name: 'Keep PuTTY sessions in sync' }))
    await userEvent.click(screen.getByRole('button', { name: /Save PuTTY settings/ }))
    await waitFor(() => expect(saveConfig).toHaveBeenCalled())
    expect(saveConfig.mock.calls[0][0]).toEqual({ ssh: { user: 'ops' }, putty: { sync: true, keepalive: 30 } })
  })

  it('says it is Windows-only elsewhere', async () => {
    status.mockResolvedValue({ ...baseStatus, supported: false, sessions: [] })
    render(<PuttySync />)
    expect(await screen.findByText(/only on Windows/)).toBeTruthy()
    expect((screen.getByRole('button', { name: /Sync now/ }) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('quickConnectOptions', () => {
  it('lists every host grouped, ungrouped last, without local', () => {
    const opts = quickConnectOptions([
      { name: 'web-02', hostname: 'w2', group: 'prod' },
      { name: 'local', hostname: '' },
      { name: 'dev-1', hostname: 'd1' },
      { name: 'web-01', hostname: 'w1', group: 'prod' },
    ] as never, ['web-01'])
    expect(opts.map(o => o.label)).toEqual(['prod', 'Ungrouped'])
    expect((opts[0] as { options: { value: string }[] }).options.map(o => o.value)).toEqual(['web-01', 'web-02'])
  })
  it('is a flat list when there are no groups', () => {
    const opts = quickConnectOptions([{ name: 'b', hostname: 'b' }, { name: 'a', hostname: 'a' }] as never, [])
    expect(opts.map(o => o.value)).toEqual(['a', 'b'])
  })
})

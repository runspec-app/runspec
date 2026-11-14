/**
 * The Schedules tab's webhook strips and the hook editor: connection + mutes,
 * muting reaches the host, the follow offer, and editing a hook (test, save
 * with a token shown once, deployed hooks read-only, settings send only what
 * changed).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { WebhookHook, WebhookVenv, WebhooksStatus } from '../bridge'
import type { SchedulerVenv } from './allSchedules'

const fns = vi.hoisted(() => ({
  webhook_mute: vi.fn(), webhook_unmute: vi.fn(), webhook_follow: vi.fn(), webhook_hooks: vi.fn(),
  webhook_hook_save: vi.fn(), webhook_hook_test: vi.fn(), webhook_hook_remove: vi.fn(), webhook_settings: vi.fn(),
}))
vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: (_t, p) => (fns as Record<string, unknown>)[p as string] ?? vi.fn() }),
}))

import { WebhooksSection, followOffers, followsText, setupTargets } from './WebhooksSection'
import { changed, hasUsername, tomlName, NEW_HOOK_TOML } from './WebhookHooksModal'

const strip = (patch: Partial<WebhookVenv> = {}): WebhookVenv => ({
  key: 'web-01/ops', host: 'web-01', venv: 'ops', follows: ['*'], connected: true, error: '', paused: '', who: 'ops',
  settings: {}, hooks: [], mutes: [], open: [], dismissed: 0, away: '',
  recent: [{ id: 3, hook: 'alerts', received: new Date().toISOString(), source: 'x', state: 'ran', exit_code: 0, fields: {}, rule: 'disk', runnable: 'cleanup-tmp', command: [], args: {}, autonomy: 'autonomous', reason: '' }],
  ...patch,
})
const sv = (host: string, venv: string, enabled: boolean | null): SchedulerVenv => ({
  key: `${host}/${venv}`, host, hostGroup: '', venv, backend: 'daemon', owner: 'ops', settings: null, hostZone: '', packages: null,
  webhooks: enabled === null ? null : { enabled, hooks: enabled ? ['alerts'] : [] },
})
const status = (venvs: WebhookVenv[], follows: Record<string, string[]> = { 'web-01': ['*'] }): WebhooksStatus => ({ configured: true, away: '', venvs, follows })

const consoleHook: WebhookHook = {
  name: 'alerts', enabled: true, description: '', source: 'console', file: '/v/runspec_scheduler.toml', managed_by: '', auth: ['token'],
  token_header: '', allow: [], fields: {}, split: '', rules: [], dedup: '', require_approval: false, transform: '',
  toml: '[[hook]]\nname = "alerts"\n', url: 'https://web-01:8730/hook/alerts',
}
const deployedHook: WebhookHook = { ...consoleHook, name: 'deploys', source: 'deployed', file: '/v/hooks.d/deploys.toml', managed_by: 'ansible', toml: '[[hook]]\nname = "deploys"\n' }

beforeEach(() => {
  Object.values(fns).forEach(f => f.mockReset())
  fns.webhook_hooks.mockResolvedValue({
    ok: true, settings: { enabled: true, port: 8730, approve_within: '15m' }, smtp: { relay: '', to: [] }, hooks: [consoleHook, deployedHook],
  })
})

describe('pure helpers', () => {
  it('offers hosts that take webhooks but are not followed', () => {
    const venvs = [sv('web-01', 'ops', true), sv('db-01', 'ops', true), sv('db-02', 'ops', false), sv('old-01', 'ops', null)]
    expect(followOffers(venvs, status([]))).toEqual(['db-01'])
    expect(setupTargets(venvs, status([strip()])).map(t => t.host)).toEqual(['db-01', 'db-02'])
    expect(followsText(['*'])).toBe('follows every hook')
    expect(followsText(['a', 'b'])).toBe('follows a, b')
  })
  it('reads a hook name and username from TOML; settings diff only what changed', () => {
    expect(tomlName(NEW_HOOK_TOML)).toBe('new-hook')
    expect(hasUsername('username = "ci"')).toBe(true)
    expect(hasUsername(NEW_HOOK_TOML)).toBe(false)
    expect(changed({ port: 1, bind: 'a', to: ['x'] }, { port: 2, bind: 'a', to: ['x'] }, ['port', 'bind', 'to'])).toEqual({ port: 2 })
  })
})

describe('WebhooksSection', () => {
  it('shows each followed venv with its last delivery and mutes', async () => {
    fns.webhook_unmute.mockResolvedValue({ ok: true })
    render(<WebhooksSection editable venvs={[]} status={status([strip({ mutes: [{ who: 'ops', hook: '*', until: new Date(Date.now() + 3600_000).toISOString(), reason: 'away' }] })])} />)
    expect(screen.getByText('webhooks following')).toBeTruthy()
    expect(screen.getByText(/Last delivery .*: alerts, ran/)).toBeTruthy()
    const muted = screen.getByText(/all hooks muted until/)
    await userEvent.click(within(muted.closest('.ant-tag') as HTMLElement).getByLabelText('Close'))
    expect(fns.webhook_unmute).toHaveBeenCalledWith({ host: 'web-01', venv: 'ops', hook: '*' })
  })

  it('mutes every hook for a chosen time', async () => {
    fns.webhook_mute.mockResolvedValue({ ok: true })
    render(<WebhooksSection editable venvs={[]} status={status([strip()])} />)
    await userEvent.click(screen.getByRole('button', { name: /Mute/ }))
    await userEvent.click(await screen.findByText('Mute every hook for 4 hours'))
    expect(fns.webhook_mute).toHaveBeenCalledWith({ host: 'web-01', venv: 'ops', hook: '*', for: '4h', reason: 'muted from the console' })
  })

  it('follows a host from the offer', async () => {
    fns.webhook_follow.mockResolvedValue({ ok: true, hooks: ['*'] })
    render(<WebhooksSection editable venvs={[sv('db-01', 'ops', true)]} status={status([])} />)
    await userEvent.click(screen.getByRole('button', { name: 'Follow webhooks on db-01' }))
    expect(fns.webhook_follow).toHaveBeenCalledWith({ host: 'db-01', hooks: ['*'] })
  })

  it('renders nothing with nothing followed and nothing to offer', () => {
    const { container } = render(<WebhooksSection editable venvs={[]} status={status([], {})} />)
    expect(container.firstChild).toBeNull()
  })
})

describe('hook editor', () => {
  const open = async () => {
    render(<WebhooksSection editable venvs={[]} status={status([strip()])} />)
    await userEvent.click(screen.getByRole('button', { name: /Hooks…/ }))
    await screen.findByLabelText('Hook as TOML')
  }

  it('saves a new token and shows it once', async () => {
    fns.webhook_hook_save.mockResolvedValue({ ok: true, hook: 'alerts', url: 'https://web-01:8730/hook/alerts', secrets: { token: 'tok-123' }, note: 'Shown once.' })
    await open()
    await userEvent.click(screen.getByLabelText('Replace the token'))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(fns.webhook_hook_save).toHaveBeenCalledWith({ host: 'web-01', venv: 'ops', text: consoleHook.toml, new_token: true, new_password: false })
    const box = await screen.findByTestId('webhook-secrets')
    expect(box.textContent).toContain('tok-123')
    await userEvent.click(within(box).getByRole('button', { name: "I've copied them" }))
    await waitFor(() => expect(screen.queryByTestId('webhook-secrets')).toBeNull())
  })

  it('tests the hook as edited', async () => {
    fns.webhook_hook_test.mockResolvedValue({ ok: true, source: 'last delivery', items: [{ state: 'pending', fields: {}, rule: 'svc', runnable: 'system-service', command: ['restart'], args: { name: 'nginx' } }] })
    await open()
    await userEvent.click(screen.getByText('Try it with a body'))
    await userEvent.click(screen.getByRole('button', { name: /Test/ }))
    const res = await screen.findByTestId('webhook-test-result')
    expect(res.textContent).toContain('would wait for approval')
    expect(res.textContent).toContain('system-service restart --name nginx')
    expect(fns.webhook_hook_test).toHaveBeenCalledWith({ host: 'web-01', venv: 'ops', text: consoleHook.toml, body: '' })
  })

  it('keeps a deployed hook read-only', async () => {
    await open()
    await userEvent.click(screen.getByRole('button', { name: /deploys/ }))
    expect((screen.getByLabelText('Hook as TOML') as HTMLTextAreaElement).readOnly).toBe(true)
    expect(screen.getByText(/managed by ansible/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('saves only the listener settings that changed', async () => {
    fns.webhook_settings.mockResolvedValue({ ok: true })
    await open()
    await userEvent.click(screen.getByRole('tab', { name: 'Listener & mail' }))
    const approve = screen.getByLabelText(/Approve within/)
    await userEvent.clear(approve)
    await userEvent.type(approve, '30m')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(fns.webhook_settings).toHaveBeenCalledWith({ host: 'web-01', venv: 'ops', hooks: { approve_within: '30m' }, smtp: {} })
  })
})

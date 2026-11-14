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

import { WebhooksSection, WebhooksSummary, followsText, hookStateText, hostOffers, isOffQuietly, portsOnHost, quietCount, quietHosts, stripHint, venvHookState, webhookCounts } from './WebhooksSection'
import { changed, hasUsername, tomlName, NEW_HOOK_TOML, suggestPort, portHint, DEFAULT_WEBHOOK_PORT } from './WebhookHooksModal'

const strip = (patch: Partial<WebhookVenv> = {}): WebhookVenv => ({
  key: 'web-01/ops', host: 'web-01', venv: 'ops', follows: ['*'], connected: true, error: '', paused: '', who: 'ops',
  settings: {}, hooks: [], mutes: [], open: [], dismissed: 0, away: '',
  recent: [{ id: 3, hook: 'alerts', received: new Date().toISOString(), source: 'x', state: 'ran', exit_code: 0, fields: {}, rule: 'disk', runnable: 'cleanup-tmp', command: [], args: {}, autonomy: 'autonomous', reason: '' }],
  ...patch,
})
const sv = (host: string, venv: string, enabled: boolean | null, hooks: string[] = enabled ? ['alerts'] : []): SchedulerVenv => ({
  key: `${host}/${venv}`, host, hostGroup: '', venv, backend: 'daemon', owner: 'ops', settings: null, hostZone: '', packages: null,
  webhooks: enabled === null ? null : { enabled, hooks },
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
  it('picks a listener port clear of the other venvs on the host', () => {
    expect(suggestPort([])).toBe(DEFAULT_WEBHOOK_PORT)
    expect(suggestPort([8977, 8978, 9000])).toBe(8979)
    expect(portHint([], false)).toBe('Each venv on a host needs its own port.')
    expect(portHint([8978, 8977, 8977], false)).toBe('Other venvs on this host use 8977, 8978.')
    expect(portHint([8977], true)).toMatch(/Another venv on this host uses this port/)
    const st = status([strip({ key: 'web-01/a', venv: 'a', settings: { port: 8977 } }), strip({ key: 'web-01/ops', settings: { port: 9100 } })])
    const vs = [{ ...sv('web-01', 'b', true), webhooks: { enabled: true, hooks: [], port: 8978 } }, { ...sv('db-01', 'c', true), webhooks: { enabled: true, hooks: [], port: 8979 } }]
    expect(portsOnHost(st, vs, 'web-01', 'ops')).toEqual([8977, 8978])
  })
  it('groups quiet venvs by host, hosts that can be turned on first', () => {
    const off = strip({ key: 'z/ops', host: 'z', settings: { enabled: false }, recent: [] })
    const root = strip({ key: 'a/root', host: 'a', venv: 'root', recent: [], unavailable: 'root-owned venv' })
    const hosts = quietHosts([off], [root])
    expect(hosts.map(h => [h.host, h.off.length, h.unavailable.length])).toEqual([['z', 1, 0], ['a', 0, 1]])
    expect(quietCount(hosts)).toBe('2 hosts, 2 venvs')
    expect(quietCount([{ host: 'a', off: [off], unavailable: [] }])).toBe('1 host, 1 venv')
  })
  it('lists every unfollowed host, followable ones first, whatever their webhooks say', () => {
    const venvs = [sv('web-01', 'ops', true), sv('db-01', 'ops', true), sv('db-02', 'ops', false), sv('old-01', 'ops', null), sv('db-02', 'tools', true, [])]
    const offers = hostOffers(venvs, status([]))
    expect(offers.map(o => [o.host, o.canFollow])).toEqual([['db-01', true], ['db-02', true], ['old-01', false]])
    expect(offers[1].venvs.map(hookStateText)).toEqual(['webhooks off', 'webhooks on, no hooks yet'])
    expect(venvHookState(sv('a', 'b', null))).toBe('old')
    expect(venvHookState(undefined)).toBe('missing')
    expect(hookStateText(sv('a', 'b', true, ['x', 'y']))).toBe('webhooks on, 2 hooks')
    expect(followsText(['*'])).toBe('follows every hook')
    expect(followsText(['a', 'b'])).toBe('follows a, b')
  })
  it('hints when a followed venv has no hooks, never because webhooks are off', () => {
    expect(stripHint(strip({ settings: { enabled: false } }))).toBeNull()
    expect(isOffQuietly(strip({ settings: { enabled: false }, recent: [] }))).toBe(true)
    // something to show keeps its strip
    expect(isOffQuietly(strip({ settings: { enabled: false } }))).toBe(false)
    expect(isOffQuietly(strip({ settings: { enabled: true }, recent: [] }))).toBe(false)
    expect(stripHint(strip({ settings: { enabled: true } }))?.action).toBe('Add a hook…')
    expect(stripHint(strip({ settings: { enabled: true }, hooks: [consoleHook] }))).toBeNull()
    expect(stripHint(strip({ connected: false, settings: { enabled: false } }))).toBeNull()
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

  it('follows a host whose webhooks are still off', async () => {
    fns.webhook_follow.mockResolvedValue({ ok: true, hooks: ['*'] })
    render(<WebhooksSection editable venvs={[sv('db-01', 'ops', false)]} status={status([], {})} />)
    const row = screen.getByTestId('webhook-host-offer')
    expect(row.textContent).toContain('webhooks off')
    await userEvent.click(within(row).getByRole('button', { name: 'Follow webhooks on db-01' }))
    expect(fns.webhook_follow).toHaveBeenCalledWith({ host: 'db-01', hooks: ['*'] })
  })

  it('says a host needs runspec-scheduler 0.11.0 and offers no Follow there', () => {
    render(<WebhooksSection editable venvs={[sv('old-01', 'ops', null)]} status={status([], {})} />)
    const row = screen.getByTestId('webhook-host-offer')
    expect(row.textContent).toContain('needs runspec-scheduler 0.11.0')
    expect((within(row).getByRole('button', { name: 'Follow webhooks on old-01' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('opens the listener settings from Set up…', async () => {
    render(<WebhooksSection editable venvs={[sv('db-01', 'ops', false)]} status={status([], {})} />)
    await userEvent.click(screen.getByRole('button', { name: 'Set up on db-01 / ops' }))
    expect(await screen.findByRole('tab', { name: 'Listener & mail', selected: true })).toBeTruthy()
    expect(fns.webhook_hooks).toHaveBeenCalledWith({ host: 'db-01', venv: 'ops' })
  })

  it('lists followed venvs with webhooks off as one row per host with a Turn on button', async () => {
    render(<WebhooksSection editable venvs={[]} status={status([
      strip(),
      strip({ key: 'web-01/tools', venv: 'tools', settings: { enabled: false }, recent: [] }),
      strip({ key: 'web-01/batch', venv: 'batch', settings: { enabled: false }, recent: [] }),
      strip({ key: 'db-01/ops', host: 'db-01', settings: { enabled: false }, recent: [] }),
    ], { 'web-01': ['*'], 'db-01': ['*'] })} />)
    expect(screen.getAllByTestId('webhook-strip')).toHaveLength(1)
    const off = screen.getByTestId('webhook-off')
    expect(off.textContent).toContain('Followed, not listening')
    expect(off.textContent).toContain('2 hosts, 3 venvs')
    const rows = screen.getAllByTestId('webhook-off-host')
    expect(rows.map(r => r.textContent)).toEqual([
      expect.stringContaining('db-01'),
      expect.stringContaining('web-01'),
    ])
    expect(within(rows[1]).getAllByRole('button', { name: /Turn on webhooks/ })).toHaveLength(2)
    expect(webhookCounts(status([strip({ settings: { enabled: false }, recent: [] })])).problems).toBe(0)
    await userEvent.click(screen.getByRole('button', { name: 'Turn on webhooks on web-01 / tools' }))
    expect(await screen.findByRole('tab', { name: 'Listener & mail', selected: true })).toBeTruthy()
  })

  it('shows no Turn on buttons when the section is read-only', () => {
    render(<WebhooksSection editable={false} venvs={[]} status={status([strip({ settings: { enabled: false }, recent: [] })])} />)
    expect(screen.getByTestId('webhook-off-host').textContent).toContain('ops')
    expect(screen.queryByRole('button', { name: /Turn on/ })).toBeNull()
  })

  it('folds many quiet hosts behind one toggle', async () => {
    const venvs = 'abcdefghi'.split('').map(h => strip({ key: `${h}/ops`, host: h, settings: { enabled: false }, recent: [] }))
    render(<WebhooksSection editable venvs={[]} status={status(venvs, Object.fromEntries('abcdefghi'.split('').map(h => [h, ['*']])))} />)
    expect(screen.queryAllByTestId('webhook-off-host')).toHaveLength(0)
    expect(screen.getByTestId('webhook-off').textContent).toContain('9 hosts, 9 venvs')
    await userEvent.click(screen.getByRole('button', { name: 'Show' }))
    expect(screen.getAllByTestId('webhook-off-host')).toHaveLength(9)
  })

  it('puts followed venvs that take no webhooks in their host row, muted', () => {
    const root = strip({ key: 'little-boy/ops-root', host: 'little-boy', venv: 'ops-root', connected: false, recent: [], unavailable: 'root-owned venv: runspec-scheduler never runs as root, so it takes no webhooks' })
    const old = strip({ key: 'little-girl/legacy', host: 'little-girl', venv: 'legacy', connected: false, recent: [], unavailable: "runspec-scheduler isn't installed in this venv (or is older than 0.11.0)" })
    render(<WebhooksSection editable venvs={[]} status={status([strip(), root, old], { 'web-01': ['*'], 'little-boy': ['*'], 'little-girl': ['*'] })} />)
    expect(screen.getAllByTestId('webhook-strip')).toHaveLength(1)
    const lines = screen.getAllByTestId('webhook-unavailable').map(e => e.textContent)
    expect(lines).toEqual(['ops-root (root-owned)', 'legacy (needs runspec-scheduler 0.11.0)'])
    expect(screen.queryByText(/A venv takes webhooks once/)).toBeNull()
    expect(screen.queryByRole('button', { name: /Turn on/ })).toBeNull()
  })

  it('folds many unfollowed hosts behind one toggle', async () => {
    const venvs = 'abcdefghi'.split('').map(h => sv(h, 'ops', true))
    render(<WebhooksSection editable venvs={venvs} status={status([], {})} />)
    expect(screen.queryAllByTestId('webhook-host-offer')).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: 'Show 9 hosts (9 can be followed)' }))
    expect(screen.getAllByTestId('webhook-host-offer')).toHaveLength(9)
  })

  it('sums up what is followed in one line for the Schedules tab', async () => {
    const pending = { ...strip().recent[0], id: 9, state: 'pending' }
    const st = status([strip({ open: [pending] }), strip({ key: 'db-01/ops', host: 'db-01', connected: false, error: 'no route to host' })], { 'web-01': ['*'], 'db-01': ['*'] })
    expect(webhookCounts(st)).toEqual({ hosts: 2, waiting: 1, problems: 1 })
    const open = vi.fn()
    render(<WebhooksSummary status={st} onOpen={open} />)
    expect(screen.getByTestId('webhooks-summary').textContent).toContain('following 2 hosts')
    await userEvent.click(screen.getByRole('button', { name: 'Open Webhooks' }))
    expect(open).toHaveBeenCalled()
  })

  it('says nothing on the Schedules tab when nothing is followed', () => {
    const { container } = render(<WebhooksSummary status={status([], {})} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing with nothing followed and no scheduler venvs', () => {
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

  it('Turn on starts the form switched on with a free port, and saves both', async () => {
    fns.webhook_hooks.mockResolvedValue({ ok: true, settings: { enabled: false, port: 0 }, smtp: {}, hooks: [] })
    fns.webhook_settings.mockResolvedValue({ ok: true })
    render(<WebhooksSection editable venvs={[]} status={status([
      strip({ key: 'web-01/tools', venv: 'tools', settings: { enabled: true, port: 8977 } }),
      strip({ settings: { enabled: false, port: 0 }, recent: [] }),
    ])} />)
    await userEvent.click(screen.getByRole('button', { name: 'Turn on webhooks on web-01 / ops' }))
    const port = (await screen.findAllByRole('spinbutton', { name: /Port/ }))[0] as HTMLInputElement
    expect(port.value).toBe('8978')
    expect(screen.getByText('Other venvs on this host use 8977.')).toBeTruthy()
    expect(screen.getByRole('switch', { checked: true })).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(fns.webhook_settings).toHaveBeenCalledWith({ host: 'web-01', venv: 'ops', hooks: { enabled: true, port: 8978 }, smtp: {} })
  })

  it('keeps what is typed when the section re-renders with fresh status', async () => {
    fns.webhook_hooks.mockResolvedValue({ ok: true, settings: { enabled: true, port: 0 }, smtp: {}, hooks: [] })
    const { rerender } = render(<WebhooksSection editable venvs={[]} status={status([strip()])} />)
    await userEvent.click(screen.getByRole('button', { name: /Hooks…/ }))
    await userEvent.click(await screen.findByRole('tab', { name: 'Listener & mail' }))
    const port = screen.getAllByRole('spinbutton', { name: /Port/ })[0] as HTMLInputElement
    expect(port.value).toBe('')
    await userEvent.type(port, '9001')
    rerender(<WebhooksSection editable venvs={[]} status={status([strip()])} />)
    await waitFor(() => expect(fns.webhook_hooks).toHaveBeenCalledTimes(1))
    expect((screen.getAllByRole('spinbutton', { name: /Port/ })[0] as HTMLInputElement).value).toBe('9001')
  })
})

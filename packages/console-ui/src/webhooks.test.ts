import { describe, it, expect } from 'vitest'
import type { WebhookDelivery, WebhookVenv, WebhooksStatus } from './bridge'
import {
  applyPush, connection, deliveryStatus, flatFields, inFlight, muteLabel, relTime, runLine, unfollowedHosts, waitingApprovals,
} from './webhooks'

const d = (id: number, patch: Partial<WebhookDelivery> = {}): WebhookDelivery => ({
  id, hook: 'alerts', received: `2026-11-14T10:0${id}:00Z`, source: '10.0.0.1', state: 'pending', fields: {},
  rule: 'svc', runnable: 'system-service', command: ['restart'], args: { name: 'nginx' }, autonomy: 'confirm', reason: '', ...patch,
})
const v = (key: string, patch: Partial<WebhookVenv> = {}): WebhookVenv => ({
  key, host: key.split('/')[0], venv: key.split('/')[1], follows: ['*'], connected: true, error: '', paused: '', who: 'ops',
  settings: {}, hooks: [], mutes: [], open: [], dismissed: 0, recent: [], away: '', ...patch,
})
const status = (venvs: WebhookVenv[]): WebhooksStatus => ({ configured: true, away: '', venvs, follows: { 'web-01': ['*'] } })

describe('waiting approvals', () => {
  it('lists pending deliveries oldest first across venvs, skipping queued and approved ones', () => {
    const s = status([
      v('web-01/ops', { open: [d(3), d(2, { state: 'queued' })] }),
      v('db-01/ops', { open: [d(1), d(4, { state: 'approved', decided_by: 'alice' })] }),
    ])
    expect(waitingApprovals(s).map(w => [w.venv.host, w.delivery.id])).toEqual([['db-01', 1], ['web-01', 3]])
    expect(inFlight(s).map(w => w.delivery.id)).toEqual([4])
  })
})

describe('connection', () => {
  it('reads paused, following, an error and connecting', () => {
    expect(connection(v('a/b', { paused: 'away' })).label).toBe('paused')
    expect(connection(v('a/b')).tone).toBe('ok')
    expect(connection(v('a/b', { connected: false, error: 'sudo refused' }))).toEqual({ label: 'not connected', tone: 'failed', detail: 'sudo refused' })
    expect(connection(v('a/b', { connected: false })).label).toBe('connecting')
  })
})

describe('delivery status', () => {
  it('turns a non-zero exit into failed and keeps decisions calm', () => {
    expect(deliveryStatus(d(1, { state: 'ran', exit_code: 0 })).tone).toBe('ok')
    expect(deliveryStatus(d(1, { state: 'ran', exit_code: 2 }))).toEqual({ label: 'failed (exit 2)', tone: 'failed' })
    expect(deliveryStatus(d(1, { state: 'denied' })).tone).toBe('neutral')
    expect(deliveryStatus(d(1)).label).toBe('waiting for approval')
  })
})

describe('run line', () => {
  it('builds the command line, quoting what needs it', () => {
    expect(runLine(d(1))).toBe('system-service restart --name nginx')
    expect(runLine({ runnable: 'x', args: { force: true, quiet: false, msg: "it's down", tags: ['a', 'b'] } }))
      .toBe("x --force --msg 'it'\\''s down' --tags a --tags b")
    expect(runLine({ runnable: '' })).toBe('')
  })
})

describe('helpers', () => {
  it('flattens fields one level per dot', () => {
    expect(flatFields({ a: { b: 1, c: 'x' }, d: [1, 2] })).toEqual([['a.b', '1'], ['a.c', 'x'], ['d', '[1,2]']])
  })
  it('says how long ago or how long until', () => {
    const now = Date.parse('2026-11-14T10:10:00Z')
    expect(relTime('2026-11-14T10:07:00Z', now)).toBe('3m ago')
    expect(relTime('2026-11-14T10:22:00Z', now)).toBe('in 12m')
    expect(relTime(null, now)).toBe('')
  })
  it('names mutes and unfollowed hosts', () => {
    expect(muteLabel('*')).toBe('all hooks')
    expect(unfollowedHosts(status([]), ['web-01', 'db-01', 'db-01'])).toEqual(['db-01'])
  })
  it('applies a push: replaces a venv, adds a new one, or asks for a refetch', () => {
    const s = status([v('web-01/ops')])
    expect(applyPush(s, { venv: v('web-01/ops', { connected: false }) })!.venvs[0].connected).toBe(false)
    expect(applyPush(s, { venv: v('a-01/x') })!.venvs.map(x => x.key)).toEqual(['a-01/x', 'web-01/ops'])
    expect(applyPush(s, { all: true })).toBeNull()
  })
})

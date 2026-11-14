/**
 * The Automation → Webhooks section: reads each host's runspec-scheduler status
 * (shared with Schedules) and lists every host's venvs with their webhook state,
 * or says no host has runspec-scheduler.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { Host, WebhooksStatus } from '../bridge'

const fns = vi.hoisted(() => ({ shared_schedules_all: vi.fn() }))
vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: (_t, p) => (fns as Record<string, unknown>)[p as string] ?? vi.fn() }),
}))

import { WebhooksView } from './WebhooksView'
import { _resetHostReadCache } from './SchedulesView'

const hosts = [{ name: 'local', connected: true }, { name: 'web-01', connected: true }] as Host[]
const none: WebhooksStatus = { configured: false, away: '', venvs: [], follows: {} }
const venv = (host: string, group: string, hooks?: { enabled: boolean; names: string[] }) => ({
  host, group, available: true, ok: true, owner: 'ops',
  status: { schedules: [], ...(hooks ? { hooks: { ok: true, settings: { enabled: hooks.enabled }, hooks: hooks.names.map(name => ({ name })) } } : {}) },
})

beforeEach(() => { _resetHostReadCache(); fns.shared_schedules_all.mockReset() })

describe('WebhooksView', () => {
  it("lists each host's venvs with what they need", async () => {
    fns.shared_schedules_all.mockResolvedValue([venv('web-01', 'ops', { enabled: false, names: [] }), venv('local', 'tools')])
    render(<WebhooksView hosts={hosts} runnables={[]} status={none} />)
    const rows = await screen.findAllByTestId('webhook-host-offer', {}, { timeout: 4000 })
    const text = rows.map(r => r.textContent).join(' | ')
    expect(text).toContain('web-01')
    expect(text).toContain('webhooks off')
    expect(text).toContain('needs runspec-scheduler 0.11.0')
    expect(fns.shared_schedules_all).toHaveBeenCalledWith(['local', 'web-01'])
  })

  it('says so when no host has runspec-scheduler', async () => {
    fns.shared_schedules_all.mockResolvedValue([{ host: 'web-01', group: 'ops', available: false, ok: true }])
    render(<WebhooksView hosts={hosts} runnables={[]} status={none} />)
    expect(await screen.findByText(/none takes webhooks/, {}, { timeout: 4000 })).toBeTruthy()
  })
})

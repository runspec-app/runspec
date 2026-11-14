/**
 * Webhook approval cards: what they show, Approve / Deny reach the host, a
 * lost race is information not an error, and nothing renders with nothing waiting.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { WebhookDelivery, WebhookVenv, WebhooksStatus } from '../bridge'

const { decide, dismiss, info } = vi.hoisted(() => ({ decide: vi.fn(), dismiss: vi.fn(), info: vi.fn() }))
vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => (p === 'webhook_decide' ? decide : p === 'webhook_dismiss' ? dismiss : vi.fn()),
  }),
}))
vi.mock('antd', async (orig) => {
  const real = await orig<typeof import('antd')>()
  return { ...real, message: { ...real.message, info, error: vi.fn(), open: vi.fn(), destroy: vi.fn() } }
})

import { WebhookApprovals, SHOWN_AT_ONCE } from './WebhookApprovals'

const d = (id: number, patch: Partial<WebhookDelivery> = {}): WebhookDelivery => ({
  id, hook: 'alerts', received: new Date(Date.now() - 120_000).toISOString(), source: '10.2.4.17', state: 'pending',
  fields: { alertname: 'nginxDown', labels: { severity: 'page' } }, rule: 'service', runnable: 'system-service',
  command: ['restart'], args: { name: 'nginx' }, autonomy: 'confirm', reason: '', expires: new Date(Date.now() + 600_000).toISOString(), ...patch,
})
const venv = (open: WebhookDelivery[]): WebhookVenv => ({
  key: 'web-01/ops', host: 'web-01', venv: 'ops', follows: ['*'], connected: true, error: '', paused: '', who: 'ops',
  settings: {}, hooks: [], mutes: [], open, dismissed: 0, recent: [], away: '',
})
const status = (open: WebhookDelivery[]): WebhooksStatus => ({ configured: true, away: '', venvs: [venv(open)], follows: {} })

beforeEach(() => { decide.mockReset(); dismiss.mockReset(); info.mockReset() })

describe('WebhookApprovals', () => {
  it('renders nothing when nothing waits', () => {
    const { container } = render(<WebhookApprovals status={status([])} />)
    expect(container.firstChild).toBeNull()
  })

  it('shows the hook, where it came from and what it wants to run', async () => {
    render(<WebhookApprovals status={status([d(41)])} />)
    expect(screen.getByText('alerts')).toBeTruthy()
    expect(screen.getByText('web-01 / ops')).toBeTruthy()
    expect(screen.getByText('system-service restart --name nginx')).toBeTruthy()
    expect(screen.getByText(/received 2m ago · from 10.2.4.17/)).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: /Show 2 fields/ }))
    expect(screen.getByText('labels.severity')).toBeTruthy()
  })

  it('approves with the note', async () => {
    decide.mockResolvedValue({ ok: true, result: { ok: true } })
    render(<WebhookApprovals status={status([d(41)])} />)
    await userEvent.type(screen.getByLabelText('Note for the record'), 'known issue')
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    expect(decide).toHaveBeenCalledWith({ host: 'web-01', venv: 'ops', id: 41, verdict: 'approve', note: 'known issue' })
  })

  it('says so when someone else decided first', async () => {
    decide.mockResolvedValue({ ok: false, error: 'delivery 41 can\'t be approved: already denied by bob', result: { ok: false, won: false } })
    render(<WebhookApprovals status={status([d(41)])} />)
    await userEvent.click(screen.getByRole('button', { name: 'Deny' }))
    await waitFor(() => expect(info).toHaveBeenCalledWith(expect.stringContaining('already denied by bob')))
  })

  it('folds cards past the first few', async () => {
    const many = Array.from({ length: SHOWN_AT_ONCE + 2 }, (_, i) => d(i + 1))
    render(<WebhookApprovals status={status(many)} />)
    expect(screen.getAllByTestId('webhook-card')).toHaveLength(SHOWN_AT_ONCE)
    await userEvent.click(screen.getByRole('button', { name: '2 more webhooks waiting' }))
    expect(screen.getAllByTestId('webhook-card')).toHaveLength(SHOWN_AT_ONCE + 2)
  })

  it('shows a delivery decided elsewhere as a quiet line', () => {
    render(<WebhookApprovals status={status([d(7, { state: 'running', decided_by: 'alice' })])} />)
    expect(screen.queryByTestId('webhook-card')).toBeNull()
    expect(screen.getByTestId('webhook-decided').textContent).toContain('approved by alice')
  })
})

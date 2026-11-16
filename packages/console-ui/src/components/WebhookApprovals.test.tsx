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

import { WebhookApprovals, SHOWN_AT_ONCE, approvalWhy } from './WebhookApprovals'

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

describe('approvalWhy', () => {
  it('names the hook when it asked for the approval, whatever the level', () => {
    expect(approvalWhy({ autonomy: 'autonomous', reason: 'the hook asks for an approval' })).toBe('This hook asks for a person to approve each run.')
    expect(approvalWhy({ autonomy: 'confirm', reason: 'the hook asks for an approval' })).toBe('This hook asks for a person to approve each run.')
  })
  it("falls back to the runnable's level (runspec-scheduler 0.11.0)", () => {
    expect(approvalWhy({ autonomy: 'confirm', reason: 'demo needs an approval (confirm)' })).toBe('Asks before running')
    expect(approvalWhy({ autonomy: 'autonomous', reason: '' })).toBe('')
  })
})

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
    // A few fields show open: they're read before Approve.
    expect(screen.getByText('labels.severity')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: /Hide the 2 fields/ }))
    expect(screen.queryByText('labels.severity')).toBeNull()
  })

  it('approves with the note', async () => {
    decide.mockResolvedValue({ ok: true, result: { ok: true } })
    render(<WebhookApprovals status={status([d(41)])} />)
    await userEvent.click(screen.getByRole('button', { name: 'Add a reason…' }))
    await userEvent.type(screen.getByLabelText('Note for the record (optional)'), 'known issue')
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

describe('WebhookPointer', () => {
  it('says how many wait and opens the Webhooks tab', async () => {
    const { WebhookPointer } = await import('./WebhookApprovals')
    const onReview = vi.fn()
    render(<WebhookPointer count={2} onReview={onReview} />)
    expect(screen.getByTestId('webhook-pointer').textContent).toContain('2 webhooks waiting for approval')
    await userEvent.click(screen.getByRole('button', { name: 'Review →' }))
    expect(onReview).toHaveBeenCalled()
  })

  it('an expired approval can only be hidden: it says so and offers no Approve', async () => {
    dismiss.mockResolvedValue({ ok: true })
    render(<WebhookApprovals status={status([d(7, { expires: new Date(Date.now() - 60_000).toISOString() })])} />)
    expect(screen.getByTestId('webhook-expired').textContent).toMatch(/expired before anyone answered/)
    expect(screen.queryByRole('button', { name: /Approve/ })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Hide' }))
    await waitFor(() => expect(dismiss).toHaveBeenCalled())
  })
})

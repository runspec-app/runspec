/**
 * The Console's one Activity control: runs in flight (with Stop, which asks)
 * and sessions waiting for you, in place of the strip above the session row.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

const { cancel } = vi.hoisted(() => ({ cancel: vi.fn(() => Promise.resolve()) }))
vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: (_t, p) => (p === 'cancel_invocation' ? cancel : vi.fn(() => Promise.resolve([]))) }),
}))

import { ActivityControl, activitySummary } from './ConsoleView'
import type { InFlightRecord } from '../bridge'

const RUN: InFlightRecord = {
  id: 'r1', runnable: 'backup', group: 'ops', host: 'prod-1', operator: 'Jason', runAs: 'svc',
  startedAt: new Date().toISOString(), args: {},
}

describe('activitySummary', () => {
  it('counts what is running and waiting', () => {
    expect(activitySummary(2, 1)).toBe('2 running · 1 waiting')
    expect(activitySummary(0, 3)).toBe('3 waiting')
    expect(activitySummary(1, 0)).toBe('1 running')
    expect(activitySummary(2, 1, 2)).toBe('2 on hosts · 2 chats working · 1 waiting')
  })
})

describe('ActivityControl', () => {
  it('shows nothing when nothing is in flight', () => {
    render(<ActivityControl inFlight={[]} waiting={[]} onOpen={() => {}} />)
    expect(screen.queryByTestId('activity-control')).toBeNull()
  })

  it('lists runs and waiting sessions; Stop asks first, Open selects', async () => {
    const onOpen = vi.fn()
    render(<ActivityControl inFlight={[RUN]} waiting={[{ id: 's2', label: 'nightly-backup' }]} onOpen={onOpen} />)
    const btn = screen.getByTestId('activity-control')
    expect(btn.getAttribute('aria-label')).toBe('Activity: 1 running · 1 waiting')
    fireEvent.click(btn)
    expect(await screen.findByText('/backup')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Stop /backup' }))
    expect(cancel).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', { name: 'Stop run' }))
    await waitFor(() => expect(cancel).toHaveBeenCalledWith('r1'))
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    expect(onOpen).toHaveBeenCalledWith('s2')
  })
})

describe('ActivityControl counts', () => {
  it('counts chats working with runs, and uses the footer’s waiting number', async () => {
    const onOpenWebhooks = vi.fn()
    render(<ActivityControl inFlight={[RUN]} working={[{ id: 's1', label: 'restart nginx' }]} waiting={[]}
      waitingTotal={2} webhookWaiting={2} onOpenWebhooks={onOpenWebhooks} onOpen={() => {}} />)
    const btn = screen.getByTestId('activity-control')
    expect(btn.getAttribute('aria-label')).toBe('Activity: 1 on hosts · 1 chat working · 2 waiting')
    fireEvent.click(btn)
    expect(await screen.findByText('restart nginx')).toBeTruthy()
    expect(screen.getByText('2 webhooks')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Review' }))
    expect(onOpenWebhooks).toHaveBeenCalled()
  })
})

describe('ActivityControl keyboard', () => {
  it('closes on Escape', async () => {
    render(<ActivityControl inFlight={[RUN]} waiting={[]} onOpen={() => {}} />)
    fireEvent.click(screen.getByTestId('activity-control'))
    expect(await screen.findByTestId('activity-list')).toBeTruthy()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.getByTestId('activity-control').getAttribute('aria-expanded') ?? 'false').not.toBe('true'))
    expect(document.activeElement).toBe(screen.getByTestId('activity-control'))
  })
})

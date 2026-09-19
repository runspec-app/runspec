import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DisclaimerGate } from './DisclaimerGate'
import type { Disclaimer } from '../bridge'

const { acknowledgeDisclaimer } = vi.hoisted(() => ({
  acknowledgeDisclaimer: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (prop === 'acknowledge_disclaimer') return acknowledgeDisclaimer
        return vi.fn()
      },
    },
  ),
}))

const DISC: Disclaimer = {
  enabled: true,
  needs_ack: true,
  title: 'AI use notice',
  text: 'Verify AI output before acting. See the [policy](https://x/p).',
  acknowledge_label: 'I acknowledge',
  version: '1',
}

describe('DisclaimerGate', () => {
  beforeEach(() => {
    acknowledgeDisclaimer.mockReset().mockResolvedValue({ ok: true, needs_ack: false })
  })

  it('shows the title, wording and a single acknowledge button', async () => {
    render(<DisclaimerGate disclaimer={DISC} onAcknowledged={() => {}} />)
    await screen.findByText('AI use notice')
    expect(screen.getByText(/Verify AI output/)).toBeTruthy()
    // The markdown link renders (opens in a new tab inside the webview).
    expect(screen.getByRole('link', { name: 'policy' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'I acknowledge' })).toBeTruthy()
    // A muted hint points at the decline path (the native window close).
    expect(screen.getByText('To decline, close the window.')).toBeTruthy()
    // No in-app Close button — declining is the native window close (which keeps
    // the app's confirm_close guard). window.destroy() would bypass that guard.
    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull()
  })

  it('records the acknowledgement and notifies the parent', async () => {
    const onAck = vi.fn()
    render(<DisclaimerGate disclaimer={DISC} onAcknowledged={onAck} />)
    await userEvent.click(await screen.findByRole('button', { name: 'I acknowledge' }))
    await waitFor(() => expect(acknowledgeDisclaimer).toHaveBeenCalledTimes(1))
    expect(onAck).toHaveBeenCalledTimes(1)
  })

  it('keeps the gate up (no parent notify) when the write fails', async () => {
    acknowledgeDisclaimer.mockResolvedValue({ ok: false, needs_ack: true })
    const onAck = vi.fn()
    render(<DisclaimerGate disclaimer={DISC} onAcknowledged={onAck} />)
    await userEvent.click(await screen.findByRole('button', { name: 'I acknowledge' }))
    await waitFor(() => expect(acknowledgeDisclaimer).toHaveBeenCalledTimes(1))
    expect(onAck).not.toHaveBeenCalled()
  })

  it('renders nothing to acknowledge when needs_ack is false', () => {
    render(<DisclaimerGate disclaimer={{ ...DISC, needs_ack: false }} onAcknowledged={() => {}} />)
    expect(screen.queryByText('AI use notice')).toBeNull()
  })
})

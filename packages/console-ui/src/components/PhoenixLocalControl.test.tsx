/**
 * PhoenixLocalControl (Settings → Telemetry): reflects the local Phoenix server
 * status — an install hint when the extra is absent, a Start control when
 * stopped, and Stop + Open-in-browser when running. No embedded iframe.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { PhoenixLocalControl } from './PhoenixLocalControl'
import type { PhoenixStatus } from '../bridge'

const { phoenixStatus, phoenixStart, phoenixStop, phoenixOpen } = vi.hoisted(() => ({
  phoenixStatus: vi.fn(),
  phoenixStart: vi.fn(),
  phoenixStop: vi.fn(),
  phoenixOpen: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, p) => {
        if (p === 'phoenix_status') return phoenixStatus
        if (p === 'phoenix_start') return phoenixStart
        if (p === 'phoenix_stop') return phoenixStop
        if (p === 'phoenix_open') return phoenixOpen
        return vi.fn()
      },
    },
  ),
}))

function st(over: Partial<PhoenixStatus> = {}): PhoenixStatus {
  return { available: true, reason: '', running: false, url: '', port: 0, ...over }
}

beforeEach(() => {
  phoenixStatus.mockReset()
  phoenixStart.mockReset()
  phoenixStop.mockReset()
  phoenixOpen.mockReset().mockResolvedValue({ ok: true, url: '' })
})

describe('PhoenixLocalControl', () => {
  it('shows the install hint (reason) when the extra is absent', async () => {
    phoenixStatus.mockResolvedValue(
      st({ available: false, reason: 'Phoenix not installed — pip install runspec-console[phoenix]' }),
    )
    render(<PhoenixLocalControl />)
    expect((await screen.findAllByText(/not installed/i)).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: /start/i })).toBeNull()
  })

  it('offers Start when stopped, and calls phoenix_start', async () => {
    phoenixStatus.mockResolvedValue(st({ running: false }))
    phoenixStart.mockResolvedValue(st({ running: true, url: 'http://127.0.0.1:6006', port: 6006 }))
    render(<PhoenixLocalControl />)
    const start = await screen.findByRole('button', { name: /start/i })
    await userEvent.click(start)
    expect(phoenixStart).toHaveBeenCalledOnce()
    // No iframe anywhere — this is a Settings control, not an embed.
    await waitFor(() => screen.getByRole('button', { name: /stop/i }))
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('shows Stop + Open in browser when running, and wires them', async () => {
    phoenixStatus.mockResolvedValue(st({ running: true, url: 'http://127.0.0.1:6006', port: 6006 }))
    phoenixStop.mockResolvedValue(st({ running: false }))
    render(<PhoenixLocalControl />)
    const open = await screen.findByRole('button', { name: /open in browser/i })
    await userEvent.click(open)
    expect(phoenixOpen).toHaveBeenCalledOnce()
    await userEvent.click(screen.getByRole('button', { name: /stop/i }))
    expect(phoenixStop).toHaveBeenCalledOnce()
  })

  it('updates on a runspec:phoenix_changed event', async () => {
    phoenixStatus.mockResolvedValue(st({ running: false }))
    render(<PhoenixLocalControl />)
    await screen.findByRole('button', { name: /start/i })
    window.dispatchEvent(
      new CustomEvent('runspec:phoenix_changed', {
        detail: st({ running: true, url: 'http://127.0.0.1:6006', port: 6006 }),
      }),
    )
    await waitFor(() => screen.getByRole('button', { name: /stop/i }))
  })
})

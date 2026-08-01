/**
 * PhoenixView: reflects the local Phoenix server status — an install hint when
 * the extra is absent, a Start control when stopped, and the embedded iframe +
 * Stop control when running.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { PhoenixView } from './PhoenixView'
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

describe('PhoenixView', () => {
  it('shows the install hint when the extra is absent', async () => {
    phoenixStatus.mockResolvedValue(
      st({ available: false, reason: 'Phoenix not installed — pip install runspec-console[phoenix]' }),
    )
    render(<PhoenixView active />)
    // findByText throws if absent, so reaching the assertion means it's present.
    await screen.findByText(/isn't installed/i)
    expect(screen.getAllByText(/pip install runspec-console\[phoenix\]/).length).toBeGreaterThan(0)
  })

  it('offers Start when installed but stopped, and calls phoenix_start', async () => {
    phoenixStatus.mockResolvedValue(st({ available: true, running: false }))
    phoenixStart.mockResolvedValue(st({ running: true, url: 'http://127.0.0.1:6006', port: 6006 }))
    render(<PhoenixView active />)
    const start = await screen.findByRole('button', { name: /start/i })
    await userEvent.click(start)
    expect(phoenixStart).toHaveBeenCalledOnce()
    // After start resolves running, the iframe appears.
    await screen.findByTitle('Phoenix')
  })

  it('embeds the iframe and offers Stop when running', async () => {
    phoenixStatus.mockResolvedValue(st({ running: true, url: 'http://127.0.0.1:6006', port: 6006 }))
    phoenixStop.mockResolvedValue(st({ running: false }))
    render(<PhoenixView active />)
    const frame = (await screen.findByTitle('Phoenix')) as HTMLIFrameElement
    expect(frame.src).toContain('127.0.0.1:6006')
    const stop = screen.getByRole('button', { name: /stop/i })
    await userEvent.click(stop)
    expect(phoenixStop).toHaveBeenCalledOnce()
  })

  it('updates on a runspec:phoenix_changed event', async () => {
    phoenixStatus.mockResolvedValue(st({ running: false }))
    render(<PhoenixView active />)
    await screen.findByRole('button', { name: /start/i })
    // Dispatch a running status; the iframe should appear without another fetch.
    window.dispatchEvent(
      new CustomEvent('runspec:phoenix_changed', {
        detail: st({ running: true, url: 'http://127.0.0.1:6006', port: 6006 }),
      }),
    )
    await screen.findByTitle('Phoenix')
  })
})

/**
 * RotaView "Take a break" control: disabled when the rota is off, calls
 * start_break with the chosen minutes, shows the on-break state, and ends a break.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { RotaView } from './RotaView'

const { listTriggers, startBreak, cancelBreak } = vi.hoisted(() => ({
  listTriggers: vi.fn(),
  startBreak: vi.fn(),
  cancelBreak: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'list_triggers') return listTriggers
      if (p === 'start_break') return startBreak
      if (p === 'cancel_break') return cancelBreak
      return vi.fn()
    },
  }),
}))

// ThemeContext is used by RotaView; stub the two helpers it imports.
vi.mock('../ThemeContext', () => ({
  useIsDark: () => false,
  textColor: () => '#000',
}))

const ENABLED_ROTA = { enabled: true, source: 'console', mon: { on: true, start: '09:00', end: '17:00' } }

beforeEach(() => {
  listTriggers.mockReset().mockResolvedValue({ triggers: [], errors: [], sources: [], rota: ENABLED_ROTA, running: true, break_until: null })
  startBreak.mockReset().mockResolvedValue({ break_until: '2026-06-21T15:30:00', reminder_set: true })
  cancelBreak.mockReset().mockResolvedValue({ ok: true })
})

describe('RotaView Take a break', () => {
  it('starts a break with the default 60 minutes', async () => {
    render(<RotaView />)
    const btn = await screen.findByRole('button', { name: /Take a break/ })
    await act(async () => { await userEvent.setup().click(btn) })
    await waitFor(() => expect(startBreak).toHaveBeenCalledWith(60))
  })

  it('disables the button when the rota is off', async () => {
    listTriggers.mockResolvedValue({ triggers: [], errors: [], sources: [], rota: { enabled: false }, running: false, break_until: null })
    render(<RotaView />)
    const btn = await screen.findByRole('button', { name: /Take a break/ })
    expect((btn as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows the on-break state and ends a break', async () => {
    const future = new Date(Date.now() + 30 * 60_000).toISOString()
    listTriggers.mockResolvedValue({ triggers: [], errors: [], sources: [], rota: ENABLED_ROTA, running: true, break_until: future })
    render(<RotaView />)
    const end = await screen.findByRole('button', { name: /End break now/ })
    await act(async () => { await userEvent.setup().click(end) })
    await waitFor(() => expect(cancelBreak).toHaveBeenCalled())
  })
})

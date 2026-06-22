/**
 * RotaView "Take a break" control: disabled when the rota is off, calls
 * start_break with the chosen minutes, shows the on-break state, and ends a break.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { RotaView } from './RotaView'

const { listTriggers, startBreak, cancelBreak, saveRota, screenState } = vi.hoisted(() => ({
  listTriggers: vi.fn(),
  startBreak: vi.fn(),
  cancelBreak: vi.fn(),
  saveRota: vi.fn(),
  screenState: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'list_triggers') return listTriggers
      if (p === 'start_break') return startBreak
      if (p === 'cancel_break') return cancelBreak
      if (p === 'save_rota') return saveRota
      if (p === 'screen_state') return screenState
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
  listTriggers.mockReset().mockResolvedValue({ triggers: [], errors: [], sources: [], rota: ENABLED_ROTA, running: true, break_until: null, screen_state: 'unlocked' })
  startBreak.mockReset().mockResolvedValue({ break_until: '2026-06-21T15:30:00', reminder_set: true })
  cancelBreak.mockReset().mockResolvedValue({ ok: true })
  saveRota.mockReset().mockResolvedValue({ ok: true })
  screenState.mockReset().mockResolvedValue('unlocked')
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

  it('defaults the lock-suspend toggle on and persists it via save_rota', async () => {
    render(<RotaView />)
    const toggle = await screen.findByRole('switch', { name: /Pause reactive triggers while my screen is locked/ })
    expect((toggle as HTMLElement).getAttribute('aria-checked')).toBe('true')  // default on
    const save = await screen.findByRole('button', { name: /Save working hours/ })
    await act(async () => { await userEvent.setup().click(save) })
    await waitFor(() => expect(saveRota).toHaveBeenCalled())
    expect(saveRota.mock.calls[0][0]).toMatchObject({ lock_suspend: true })
  })

  it('shows a live lock indicator from screen_state', async () => {
    screenState.mockResolvedValue('locked')
    render(<RotaView />)
    expect(await screen.findByText(/screen locked — triggers paused/)).toBeTruthy()
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

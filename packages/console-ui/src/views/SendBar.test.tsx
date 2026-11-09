import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { SendBar } from './SendBar'
import {
  CONFIRM_OVER, describeResult, historyStep, needsConfirm, pushHistory, signature, tickedIds,
} from './sendBar'
import type { TerminalWindow } from '../bridge'

const { terminalWindows, sendToTerminals, focusTerminal } = vi.hoisted(() => ({
  terminalWindows: vi.fn(),
  sendToTerminals: vi.fn(),
  focusTerminal: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, prop) =>
        prop === 'terminal_windows' ? terminalWindows
          : prop === 'send_to_terminals' ? sendToTerminals
            : prop === 'focus_terminal' ? focusTerminal
              : vi.fn(),
    },
  ),
}))

const WINS: TerminalWindow[] = [
  { id: '10', host: 'web-01', ready: true },
  { id: '11', host: 'web-02', ready: true },
  { id: '12', host: 'db-01', ready: false },
]

describe('send bar helpers', () => {
  it('asks only past the limit and only once per set', () => {
    const many = Array.from({ length: CONFIRM_OVER + 1 }, (_, i) => String(i))
    expect(needsConfirm(many.slice(0, CONFIRM_OVER), null)).toBe(false)
    expect(needsConfirm(many, null)).toBe(true)
    expect(needsConfirm([...many].reverse(), signature(many))).toBe(false)
    expect(needsConfirm([...many, 'x'], signature(many))).toBe(true)
  })

  it('ticks every listed window except the unticked ones', () => {
    expect(tickedIds(WINS, new Set(['11', 'gone']))).toEqual(['10', '12'])
  })

  it('recalls history like a shell', () => {
    expect(historyStep(3, 3, -1)).toBe(2)
    expect(historyStep(3, 0, -1)).toBe(0)
    expect(historyStep(3, 3, 1)).toBe(3)
    expect(pushHistory(['a'], 'a')).toEqual(['a'])
    expect(pushHistory(['a'], '  ')).toEqual(['a'])
    expect(pushHistory(['a', 'b'], 'c', 2)).toEqual(['b', 'c'])
  })

  it('describes a send, naming windows it missed', () => {
    expect(describeResult({ sent: ['a', 'b'], missing: [] }, 'Sent')).toBe('Sent → 2 terminals')
    expect(describeResult({ sent: ['a'], missing: ['b'] }, 'Ctrl+C'))
      .toBe('Ctrl+C → 1 terminal. Not sent to 1 (closed or not ready): b')
  })
})

describe('SendBar', () => {
  beforeEach(() => {
    terminalWindows.mockReset().mockResolvedValue({ supported: true, windows: WINS, keys: [] })
    sendToTerminals.mockReset().mockResolvedValue({ sent: ['web-01', 'web-02'], missing: [] })
    focusTerminal.mockReset().mockResolvedValue(true)
  })

  it('sends a typed line to every ticked window and remembers it', async () => {
    const user = userEvent.setup()
    render(<SendBar />)
    await screen.findByText('web-01')
    await user.click(screen.getByRole('checkbox', { name: 'db-01' }))
    expect(screen.getByText('2 of 3 terminals ticked')).toBeTruthy()
    await user.type(screen.getByLabelText('Command'), 'uptime{Enter}')
    expect(sendToTerminals).toHaveBeenCalledWith(['10', '11'], 'uptime', true, '', false)
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Sent → 2 terminals'))
    expect((screen.getByLabelText('Command') as HTMLTextAreaElement).value).toBe('')
    await user.type(screen.getByLabelText('Command'), '{ArrowUp}')
    expect((screen.getByLabelText('Command') as HTMLTextAreaElement).value).toBe('uptime')
  })

  it('presses a named key', async () => {
    const user = userEvent.setup()
    render(<SendBar />)
    await screen.findByText('web-01')
    await user.click(screen.getByRole('button', { name: 'Ctrl+C' }))
    expect(sendToTerminals).toHaveBeenCalledWith(['10', '11', '12'], '', false, 'ctrl-c')
  })

  it('sends hidden text without keeping it in history', async () => {
    const user = userEvent.setup()
    render(<SendBar />)
    await screen.findByText('web-01')
    await user.click(screen.getByRole('button', { name: 'Hide text' }))
    await user.type(screen.getByLabelText('Hidden text'), 's3cret{Enter}')
    expect(sendToTerminals).toHaveBeenCalledWith(['10', '11', '12'], 's3cret', true, '', true)
    // Back to the normal field, and ↑ recalls nothing.
    const cmd = await screen.findByLabelText('Command')
    await user.type(cmd, '{ArrowUp}')
    expect((cmd as HTMLTextAreaElement).value).toBe('')
  })

  it('asks before sending to more than 10 windows, once', async () => {
    const many = Array.from({ length: 12 }, (_, i) => ({ id: String(i), host: `h${i}`, ready: true }))
    terminalWindows.mockResolvedValue({ supported: true, windows: many, keys: [] })
    const user = userEvent.setup()
    render(<SendBar />)
    await screen.findByText('h0')
    await user.type(screen.getByLabelText('Command'), 'id{Enter}')
    expect(sendToTerminals).not.toHaveBeenCalled()
    await user.click(await screen.findByRole('button', { name: 'Send to 12' }))
    await waitFor(() => expect(sendToTerminals).toHaveBeenCalledTimes(1))
    await user.type(screen.getByLabelText('Command'), 'id{Enter}')
    await waitFor(() => expect(sendToTerminals).toHaveBeenCalledTimes(2))
  })

  it('says it is Windows only elsewhere', async () => {
    terminalWindows.mockResolvedValue({ supported: false, windows: [], keys: [] })
    render(<SendBar />)
    expect(await screen.findByText('Sending to terminals works on Windows only.')).toBeTruthy()
  })
})

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { SendBar } from './SendBar'
import {
  CONFIRM_OVER, describeResult, historyStep, lateArrivals, planSend, pushHistory, signature, tickedIds,
} from './sendBarModel'
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
  it('plans each send by the risk ladder', () => {
    const many = Array.from({ length: CONFIRM_OVER + 1 }, (_, i) => String(i))
    const few = many.slice(0, 3)
    // Never an empty line, hidden or not; Enter has its own key button.
    expect(planSend({ ticked: few, line: '  ', remembered: null })).toEqual({ kind: 'empty' })
    expect(planSend({ ticked: few, line: '', hidden: true, remembered: null })).toEqual({ kind: 'empty' })
    expect(planSend({ ticked: few, key: 'enter', remembered: null })).toEqual({ kind: 'send' })
    // Past the limit: ask, and "don't ask again" covers exactly this set.
    expect(planSend({ ticked: many.slice(0, CONFIRM_OVER), line: 'id', remembered: null })).toEqual({ kind: 'send' })
    expect(planSend({ ticked: many, line: 'id', remembered: null })).toEqual({ kind: 'ask', disruptive: false, canRemember: true })
    expect(planSend({ ticked: [...many].reverse(), line: 'id', remembered: signature(many) })).toEqual({ kind: 'send' })
    expect(planSend({ ticked: [...many, 'x'], line: 'id', remembered: signature(many) }).kind).toBe('ask')
    // Stopping or removing something asks every time, even when remembered.
    for (const line of ['sudo systemctl restart nginx', 'rm -rf /tmp/x', 'reboot', 'ls; kill 12', 'docker rm web']) {
      expect(planSend({ ticked: few, line, remembered: signature(few) }), line).toEqual({ kind: 'ask', disruptive: true, canRemember: false })
    }
    expect(planSend({ ticked: few, key: 'ctrl-d', remembered: null }).kind).toBe('ask')
    expect(planSend({ ticked: ['1'], key: 'ctrl-c', remembered: null })).toEqual({ kind: 'send' })
    for (const line of ['systemctl status nginx', 'grep rm notes.txt', 'echo killed', 'firmware --version']) {
      expect(planSend({ ticked: few, line, remembered: null }), line).toEqual({ kind: 'send' })
    }
    // A password to a few windows goes; past the limit it asks like any line.
    expect(planSend({ ticked: few, line: 's3cret', hidden: true, remembered: null })).toEqual({ kind: 'send' })
    expect(planSend({ ticked: many, line: 's3cret', hidden: true, remembered: null })).toEqual({ kind: 'ask', disruptive: false, canRemember: true })
  })

  it('treats windows that appear after the first list as late', () => {
    expect(lateArrivals(WINS, new Set(), true)).toEqual([])
    expect(lateArrivals(WINS, new Set(['10', '11']), false)).toEqual(['12'])
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
    await user.click(screen.getByRole('checkbox', { name: /db-01 · not ready/ }))
    expect(screen.getByText('2 of 3 terminals ticked')).toBeTruthy()
    await user.type(screen.getByLabelText('Command'), 'uptime{Enter}')
    expect(sendToTerminals).toHaveBeenCalledWith(['10', '11'], 'uptime', true, '', false)
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Sent → 2 terminals'))
    expect((screen.getByLabelText('Command') as HTMLTextAreaElement).value).toBe('')
    await user.type(screen.getByLabelText('Command'), '{ArrowUp}')
    expect((screen.getByLabelText('Command') as HTMLTextAreaElement).value).toBe('uptime')
  })

  it('presses a named key; one that ends things asks first, showing the key', async () => {
    const user = userEvent.setup()
    render(<SendBar />)
    await screen.findByText('web-01')
    await user.click(screen.getByRole('button', { name: 'Tab' }))
    // The arrow keys are named, not just drawn.
    expect(screen.getByRole('button', { name: 'Up arrow' })).toBeTruthy()
    expect(sendToTerminals).toHaveBeenCalledWith(['10', '11', '12'], '', false, 'tab')
    await user.click(screen.getByRole('button', { name: 'Interrupt (Ctrl+C)' }))
    expect(sendToTerminals).toHaveBeenCalledTimes(1)
    expect((await screen.findByTestId('risk-action')).textContent).toBe('Press Interrupt (Ctrl+C)')
    // db-01 isn't open yet: it is named as not sent, not counted as a target.
    expect(screen.getByText(/Not ready, so not sent: db-01/)).toBeTruthy()
    await user.click(screen.getByRole('button', { name: 'Send to 2' }))
    await waitFor(() => expect(sendToTerminals).toHaveBeenCalledWith(['10', '11', '12'], '', false, 'ctrl-c'))
  })

  it('never sends an empty line', async () => {
    const user = userEvent.setup()
    render(<SendBar />)
    await screen.findByText('web-01')
    await user.type(screen.getByLabelText('Command'), '{Enter}')
    expect(sendToTerminals).not.toHaveBeenCalled()
    expect(screen.getByRole('status').textContent).toMatch(/Enter key button/)
  })

  it('sends hidden text without keeping it, and stays hidden until turned off', async () => {
    const user = userEvent.setup()
    render(<SendBar />)
    await screen.findByText('web-01')
    const lock = screen.getByRole('button', { name: 'Hidden text' })
    await user.click(lock)
    expect(lock.getAttribute('aria-pressed')).toBe('true')
    // An empty password isn't sent.
    await user.type(screen.getByLabelText('Hidden text', { selector: 'input' }), '{Enter}')
    expect(sendToTerminals).not.toHaveBeenCalled()
    await user.type(screen.getByLabelText('Hidden text', { selector: 'input' }), 's3cret{Enter}')
    expect(sendToTerminals).toHaveBeenCalledWith(['10', '11', '12'], 's3cret', true, '', true)
    // Still hidden: a retyped password can't go out in the clear.
    expect(screen.getByLabelText('Hidden text', { selector: 'input' })).toBeTruthy()
    await user.click(lock)
    const cmd = await screen.findByLabelText('Command')
    await user.type(cmd, '{ArrowUp}')
    expect((cmd as HTMLTextAreaElement).value).toBe('')
  })

  it('asks before sending to more than 10 windows, showing the line, Cancel focused', async () => {
    const many = Array.from({ length: 12 }, (_, i) => ({ id: String(i), host: `h${i}`, ready: true }))
    terminalWindows.mockResolvedValue({ supported: true, windows: many, keys: [] })
    const user = userEvent.setup()
    render(<SendBar />)
    await screen.findByText('h0')
    await user.type(screen.getByLabelText('Command'), 'id{Enter}')
    expect(sendToTerminals).not.toHaveBeenCalled()
    expect((await screen.findByTestId('risk-action')).textContent).toBe('id')
    await waitFor(() => expect(document.activeElement?.textContent).toBe('Cancel'))
    // Without "don't ask again" the next line asks too.
    await user.click(screen.getByRole('button', { name: 'Send to 12' }))
    await waitFor(() => expect(sendToTerminals).toHaveBeenCalledTimes(1))
    await user.type(screen.getByLabelText('Command'), 'id{Enter}')
    await user.click(await screen.findByRole('checkbox', { name: /Don't ask again/ }))
    await user.click(screen.getByRole('button', { name: 'Send to 12' }))
    await waitFor(() => expect(sendToTerminals).toHaveBeenCalledTimes(2))
    await user.type(screen.getByLabelText('Command'), 'id{Enter}')
    await waitFor(() => expect(sendToTerminals).toHaveBeenCalledTimes(3))
  })

  it('a window opened later joins unticked, marked new', async () => {
    terminalWindows.mockResolvedValueOnce({ supported: true, windows: WINS.slice(0, 2), keys: [] })
    render(<SendBar />)
    await screen.findByText('web-01')
    // the next poll lists db-01 too
    expect(await screen.findByText(/new, tick to include/, undefined, { timeout: 3000 })).toBeTruthy()
    expect((screen.getByRole('checkbox', { name: /db-01/ }) as HTMLInputElement).checked).toBe(false)
    expect(screen.getByText('2 of 3 terminals ticked')).toBeTruthy()
  })

  it('a partial send is reported in the warning colour', async () => {
    sendToTerminals.mockResolvedValue({ sent: ['web-01'], missing: ['db-01'] })
    const user = userEvent.setup()
    render(<SendBar />)
    await screen.findByText('web-01')
    await user.type(screen.getByLabelText('Command'), 'uptime{Enter}')
    const status = await screen.findByRole('status')
    await waitFor(() => expect(status.textContent).toMatch(/Not sent to 1/))
    expect(status.style.fontWeight).toBe('600')
  })

  it('says it is Windows only elsewhere', async () => {
    terminalWindows.mockResolvedValue({ supported: false, windows: [], keys: [] })
    render(<SendBar />)
    expect(await screen.findByText('Sending to terminals works on Windows only.')).toBeTruthy()
  })
})

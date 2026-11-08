/**
 * A Forms submission picked on several hosts (runspec:invoke_runnable with
 * `targets`) starts the run on each host and gives each its own run card.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'

import { ConsoleView } from './ConsoleView'
import type { ChatSessionInfo, Runnable } from '../bridge'

const { invokeRunnable, newChatSession } = vi.hoisted(() => ({
  invokeRunnable: vi.fn(),
  newChatSession: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (typeof p === 'symbol') return undefined
      switch (p) {
        case 'invoke_runnable': return invokeRunnable
        case 'new_chat_session': return newChatSession
        case 'list_chat_sessions': return () => Promise.resolve([])
        case 'get_trigger_board':
        case 'get_schedule_board':
        case 'get_pending_confirms':
          return () => Promise.resolve([])
        default: return vi.fn().mockResolvedValue(undefined)
      }
    },
  }),
}))

beforeEach(() => {
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 1 }) as typeof requestAnimationFrame
  newChatSession.mockReset().mockImplementation(async (): Promise<ChatSessionInfo> => ({
    id: 's1', kind: 'chat', label: '', interactive: true,
    created_at: Date.now() / 1000, state: 'idle', turn: '', turns: 0,
  }))
  invokeRunnable.mockReset().mockImplementation(async (host: string) => `run-${host}`)
})

const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(r => setTimeout(r, 0)) }
const rn = (host: string, group: string) => ({ name: 'restart-web', host, group, args: [] }) as unknown as Runnable

describe('Console → one Forms run on several hosts', () => {
  it('invokes on each picked host, in its own venv, with a card per host', async () => {
    render(<ConsoleView inFlight={[]} />)
    const targets = [rn('web-01', 'ops'), rn('web-02', 'ops2')]
    await act(async () => {
      window.dispatchEvent(new CustomEvent('runspec:invoke_runnable', {
        detail: { runnable: targets[0], args: { force: true }, commandPath: [], targets },
      }))
      await settle()
    })
    expect(invokeRunnable).toHaveBeenCalledTimes(2)
    expect(invokeRunnable).toHaveBeenCalledWith('web-01', 'restart-web', { force: true }, [], 'ops', undefined)
    expect(invokeRunnable).toHaveBeenCalledWith('web-02', 'restart-web', { force: true }, [], 'ops2', undefined)
    expect(screen.getByText('/restart-web on web-01')).toBeTruthy()
    expect(screen.getByText('/restart-web on web-02')).toBeTruthy()
  })
})

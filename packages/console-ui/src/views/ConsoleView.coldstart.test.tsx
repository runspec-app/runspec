/**
 * Opening the console with only automation runs on the board (no chat open)
 * starts a chat, so the first thing the composer shows isn't "read-only".
 */
import { describe, it, expect, vi } from 'vitest'
import { render, act, waitFor } from '@testing-library/react'

const { newChatSession } = vi.hoisted(() => ({
  newChatSession: vi.fn(() => Promise.resolve({ id: 's9', interactive: true, kind: 'chat', label: 'New chat', created_at: Date.now() / 1000, current: true, turns: 0 })),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (typeof p === 'symbol') return undefined
      switch (p) {
        case 'new_chat_session': return newChatSession
        case 'list_chat_sessions': return () => Promise.resolve([])
        case 'get_trigger_board': return () => Promise.resolve([])
        case 'get_schedule_board': return () => Promise.resolve([{
          card_id: 'sc1', trigger: 'nightly-backup', source: 'schedule', event_type: 'runnable',
          summary: '/backup-db on db01', column: 'done', detail: {}, created_at: Date.now() / 1000,
        }])
        case 'get_pending_confirms': return () => Promise.resolve([])
        default: return vi.fn(() => Promise.resolve(undefined))
      }
    },
  }),
}))

import { ConsoleView } from './ConsoleView'

describe('Console cold start', () => {
  it('starts a chat when only automation sessions are open', async () => {
    await act(async () => { render(<ConsoleView inFlight={[]} />) })
    await waitFor(() => expect(newChatSession).toHaveBeenCalledTimes(1))
  })
})

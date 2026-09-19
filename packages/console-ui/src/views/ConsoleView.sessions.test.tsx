/**
 * Console sessions: every conversation — an interactive chat, a trigger fire,
 * a schedule fire — is a session with its own chip; the transcript shows the
 * selected session's blocks only. These cover:
 *   1. a trigger firing while a chat streams leaves the composer on the chat
 *      (chat_active carries the chat's turn, not the trigger's) and shows both chips;
 *   2. switching chips filters the blocks and flags an automation session read-only;
 *   3. "New chat" opens another session — the old one keeps running, blocks intact;
 *   4. a schedule card creates a session + a run block filled from its result;
 *   5. tokens for a non-selected session set its unread dot; opening clears it;
 *   6. closing a session drops its blocks and tells the backend.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'

import { ConsoleView } from './ConsoleView'
import type { ChatSessionInfo, TriggerCard } from '../bridge'

const { newChatSession, selectChatSession, closeChatSession, sendChat, clearChat, listChatSessions } = vi.hoisted(() => ({
  newChatSession: vi.fn(),
  selectChatSession: vi.fn(),
  closeChatSession: vi.fn(),
  sendChat: vi.fn(),
  clearChat: vi.fn(),
  listChatSessions: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (typeof p === 'symbol') return undefined
      switch (p) {
        case 'new_chat_session': return newChatSession
        case 'select_chat_session': return selectChatSession
        case 'close_chat_session': return closeChatSession
        case 'send_chat': return sendChat
        case 'clear_chat': return clearChat
        case 'list_chat_sessions': return listChatSessions
        case 'get_trigger_board':
        case 'get_schedule_board':
        case 'get_pending_confirms':
          return () => Promise.resolve([])
        default: return vi.fn()
      }
    },
  }),
}))

// Hand-driven requestAnimationFrame queue (OutputPanel coalesces tokens per frame).
let rafCbs: FrameRequestCallback[] = []
let sessionN = 0
let chatN = 0
beforeEach(() => {
  rafCbs = []
  sessionN = 0
  chatN = 0
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { rafCbs.push(cb); return rafCbs.length }) as typeof requestAnimationFrame
  globalThis.cancelAnimationFrame = (() => {}) as typeof cancelAnimationFrame
  newChatSession.mockReset().mockImplementation(async (): Promise<ChatSessionInfo> => ({
    id: `s${++sessionN}`, kind: 'chat', label: '', interactive: true,
    created_at: Date.now() / 1000, state: 'idle', turn: '', turns: 0,
  }))
  selectChatSession.mockReset().mockResolvedValue({ ok: true })
  closeChatSession.mockReset().mockResolvedValue({ ok: true })
  sendChat.mockReset().mockImplementation(async () => `chat-${++chatN}`)
  clearChat.mockReset().mockResolvedValue({ ok: true })
  listChatSessions.mockReset().mockResolvedValue([])
})
afterEach(() => vi.restoreAllMocks())

const flushRaf = () => { const cbs = rafCbs; rafCbs = []; cbs.forEach(cb => cb(0)) }
const settle = async () => { for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0)) }

function card(over: Partial<TriggerCard>): TriggerCard {
  return {
    card_id: 'a1', trigger: 'nightly', source: 'outlook', event_type: 'new_mail',
    summary: 'A new mail', column: 'running', detail: { mode: 'agent' }, created_at: Date.now() / 1000,
    ...over,
  }
}
const emit = (name: string, detail: unknown) => window.dispatchEvent(new CustomEvent(name, { detail }))
const sendMessage = async (message: string) => {
  await act(async () => { emit('runspec:send_chat', { message }); await settle() })
}
const chips = () => screen.getAllByTestId('session-chip') as HTMLElement[]
const chip = (id: string) => chips().find(c => c.dataset.sessionId === id) as HTMLElement
const clickChip = async (id: string) => { await act(async () => { fireEvent.click(chip(id)); await settle() }) }

type Active = { id: string | null; session: string | null; readOnly: boolean }
function trackActive(): () => Active {
  let last: Active = { id: null, session: null, readOnly: false }
  window.addEventListener('runspec:chat_active', e => { last = (e as CustomEvent).detail as Active })
  return () => last
}

function mount() {
  render(<ConsoleView inFlight={[]} />)
}

describe('Console sessions', () => {
  it('keeps the composer on the chat when a trigger fires mid-stream, and shows both chips', async () => {
    mount()
    const active = trackActive()
    await sendMessage('hello')
    expect(newChatSession).toHaveBeenCalledTimes(1)
    expect(sendChat).toHaveBeenCalledWith('hello', undefined, 's1')
    expect(screen.getByText('hello')).toBeTruthy()
    expect(active()).toMatchObject({ id: 'chat-1', session: 's1', readOnly: false })

    await act(async () => { emit('runspec:trigger_card', card({})); await settle() })
    // Two chips: the chat (selected) and the trigger fire.
    expect(chips().map(c => c.dataset.sessionId)).toEqual(['s1', 'a1'])
    expect(chip('s1').dataset.selected).toBe('true')
    expect(chip('a1').dataset.status).toBe('running')
    // The trigger's turn does not take over the composer.
    expect(active()).toMatchObject({ id: 'chat-1', session: 's1', readOnly: false })
    // Its block is not in the visible transcript (only its chip carries the label).
    expect(screen.getAllByText('Trigger nightly · new_mail')).toHaveLength(1)
    expect(screen.getByText('hello')).toBeTruthy()
  })

  it('switching chips filters the transcript and marks an automation session read-only', async () => {
    mount()
    const active = trackActive()
    await sendMessage('hello')
    await act(async () => { emit('runspec:trigger_card', card({})); await settle() })

    await clickChip('a1')
    expect(chip('a1').dataset.selected).toBe('true')
    expect(screen.queryByText('hello')).toBeNull()
    // Chip + block both show the label now.
    expect(screen.getAllByText('Trigger nightly · new_mail')).toHaveLength(2)
    expect(active()).toMatchObject({ id: 'a1', session: 'a1', readOnly: true })
    // Automation sessions are selected locally — never handed to the backend.
    expect(selectChatSession).not.toHaveBeenCalled()

    await clickChip('s1')
    expect(screen.getByText('hello')).toBeTruthy()
    expect(selectChatSession).toHaveBeenCalledWith('s1')
    expect(active()).toMatchObject({ id: 'chat-1', session: 's1', readOnly: false })
  })

  it('New chat opens another session and leaves the old one running with its blocks', async () => {
    mount()
    await sendMessage('hello')
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /New chat/ }))
      await settle()
    })
    expect(newChatSession).toHaveBeenCalledTimes(2)
    expect(clearChat).not.toHaveBeenCalled()
    expect(chips().map(c => c.dataset.sessionId)).toEqual(['s1', 's2'])
    expect(chip('s2').dataset.selected).toBe('true')
    // The first chat's turn is still in flight — its chip spins, its block waits.
    expect(chip('s1').dataset.status).toBe('running')
    expect(screen.queryByText('hello')).toBeNull()
    await clickChip('s1')
    expect(screen.getByText('hello')).toBeTruthy()
    // A message sent now goes to the re-selected session.
    await sendMessage('again')
    expect(sendChat).toHaveBeenLastCalledWith('again', undefined, 's1')
  })

  it('a schedule card creates a session and a run block filled from its result', async () => {
    mount()
    const sched = card({
      card_id: 'sc1', trigger: 'nightly-backup', source: 'schedule', event_type: 'runnable',
      summary: '/backup-db on db01', detail: {},
    })
    await act(async () => { emit('runspec:schedule_card', sched); await settle() })
    expect(chip('sc1')).toBeTruthy()
    expect(chip('sc1').textContent).toContain('nightly-backup · runnable')
    // Nothing else was open, so it is selected and its block shows.
    expect(chip('sc1').dataset.selected).toBe('true')
    expect(screen.getByText('/backup-db on db01')).toBeTruthy()

    await act(async () => {
      emit('runspec:schedule_card', { ...sched, column: 'done', detail: { result: 'Backed up 3 databases' } })
      await settle()
    })
    expect(screen.getByText(/Backed up 3 databases/)).toBeTruthy()
    expect(chip('sc1').dataset.status).toBe('done')
  })

  it('tokens for a non-selected session set its unread dot; opening it clears the dot', async () => {
    mount()
    await sendMessage('hello')
    await act(async () => { emit('runspec:trigger_card', card({})); await settle() })
    expect(chip('a1').dataset.unread).toBe('false')

    await act(async () => { emit('runspec:token', { id: 'a1', token: 'Working on it' }); flushRaf(); await settle() })
    expect(chip('a1').dataset.unread).toBe('true')
    expect(screen.getByLabelText('unread')).toBeTruthy()
    // Activity in the selected session never flags it.
    expect(chip('s1').dataset.unread).toBe('false')

    await clickChip('a1')
    expect(chip('a1').dataset.unread).toBe('false')
    expect(screen.getByText(/Working on it/)).toBeTruthy()
  })

  it('closing a session removes its blocks and tells the backend', async () => {
    mount()
    await act(async () => {
      emit('runspec:trigger_card', card({ card_id: 'r1', column: 'done', detail: { mode: 'rule', tool: 'restart', result: 'restarted ok' } }))
      await settle()
    })
    expect(screen.getByText(/restarted ok/)).toBeTruthy()
    expect(chip('r1').dataset.status).toBe('done')

    await act(async () => {
      fireEvent.click(screen.getByLabelText('Close session'))
      await settle()
    })
    expect(closeChatSession).toHaveBeenCalledWith('r1')
    expect(screen.queryAllByTestId('session-chip')).toHaveLength(0)
    expect(screen.queryByText(/restarted ok/)).toBeNull()
  })
})

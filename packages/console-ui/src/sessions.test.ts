/**
 * Pure session model: folding runspec:session + run-card events into the
 * chip list, deriving a chip's status, pruning finished automation, and
 * picking the neighbour to select when a session closes.
 */
import { describe, it, expect } from 'vitest'
import {
  applySessionEvent, applyCardEvent, sessionFromCard, sessionStatus,
  pruneFinished, nextSelection, markUnread, cardLabel, type Session,
} from './sessions'
import type { ChatSessionInfo, TriggerCard } from './bridge'

const info = (over: Partial<ChatSessionInfo> = {}): ChatSessionInfo => ({
  id: 's1', kind: 'chat', label: 'hello', interactive: true, created_at: 1000,
  state: 'idle', turn: '', turns: 0, ...over,
})
const card = (over: Partial<TriggerCard> = {}): TriggerCard => ({
  card_id: 'c1', trigger: 'nightly', source: 'outlook', event_type: 'new_mail',
  summary: 'A mail', column: 'running', detail: { mode: 'agent' }, created_at: 2000, ...over,
})
const sess = (over: Partial<Session> = {}): Session => ({
  id: 's1', kind: 'chat', label: 'x', interactive: true, createdAt: 1, state: 'idle', unread: false, ...over,
})
const block = (id: string, sessionId: string, done = true) => ({ id, sessionId, done })

describe('applySessionEvent', () => {
  it('appends a new full payload and merges a known id', () => {
    let list = applySessionEvent([], info())
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({ id: 's1', kind: 'chat', interactive: true, createdAt: 1000 * 1000, state: 'idle', unread: false })
    list = markUnread(list, 's1', true)
    list = applySessionEvent(list, info({ state: 'running', turn: 'chat-9', label: 'first message' }))
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({ state: 'running', turnId: 'chat-9', label: 'first message', unread: true })
  })

  it('removes a session on the partial closed payload; ignores an unknown partial', () => {
    const list = applySessionEvent([], info())
    expect(applySessionEvent(list, { id: 's1', state: 'closed' })).toEqual([])
    expect(applySessionEvent(list, { id: 'nope', state: 'running' })).toBe(list)
  })

  it('keeps the existing label when the event carries an empty one', () => {
    const list = applySessionEvent(applySessionEvent([], info({ label: 'keep' })), info({ label: '', state: 'running' }))
    expect(list[0].label).toBe('keep')
  })
})

describe('run cards as sessions', () => {
  it('builds a read-only session from a trigger card (agent label)', () => {
    const s = sessionFromCard(card(), 'trigger')
    expect(s).toMatchObject({ id: 'c1', kind: 'trigger', interactive: false, state: 'running', column: 'running', turnId: 'c1' })
    expect(s.label).toBe('Trigger nightly · new_mail')
  })

  it('labels a rule trigger by its target and a schedule by name · event_type', () => {
    expect(cardLabel(card({ detail: { mode: 'rule', tool: 'restart' } }), 'trigger')).toBe('/restart')
    expect(cardLabel(card({ trigger: 'nightly-backup', event_type: 'runnable' }), 'schedule')).toBe('nightly-backup · runnable')
  })

  it('reconciles later card events onto the same session (terminal ⇒ done)', () => {
    let list = applyCardEvent([], card(), 'trigger')
    list = applySessionEvent(list, info({ id: 'c1', kind: 'trigger', interactive: false, label: 'backend label', state: 'running', turn: 'c1' }))
    list = applyCardEvent(list, card({ column: 'done' }), 'trigger')
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({ state: 'done', column: 'done', label: 'backend label', turnId: undefined })
  })
})

describe('sessionStatus', () => {
  it('is waiting when a confirm is parked on one of its blocks or its card needs a human', () => {
    const s = sess({ id: 'a', interactive: false, kind: 'trigger', state: 'running' })
    expect(sessionStatus(s, [block('a', 'a', false)], new Set(['a']))).toBe('waiting')
    expect(sessionStatus(sess({ id: 'b', state: 'idle' }), [block('t1', 'b', false)], ['t1'])).toBe('waiting')
    expect(sessionStatus(sess({ id: 'c', interactive: false, state: 'running', column: 'awaiting_human' }), [], [])).toBe('waiting')
    expect(sessionStatus(sess({ id: 'd', interactive: false, state: 'running', column: 'awaiting_claim' }), [], [])).toBe('waiting')
  })

  it('is running while the session or one of its blocks is in flight', () => {
    expect(sessionStatus(sess({ state: 'running' }), [], [])).toBe('running')
    expect(sessionStatus(sess({ state: 'idle' }), [block('t1', 's1', false)], [])).toBe('running')
  })

  it('is done for a finished automation session, idle otherwise', () => {
    expect(sessionStatus(sess({ interactive: false, state: 'done' }), [block('t1', 's1')], [])).toBe('done')
    expect(sessionStatus(sess(), [block('t1', 's1')], [])).toBe('idle')
    // Another session's running block does not leak.
    expect(sessionStatus(sess(), [block('t2', 'other', false)], [])).toBe('idle')
  })
})

describe('pruneFinished', () => {
  it('drops finished automation beyond the newest cap, never interactive or running', () => {
    const list: Session[] = [
      sess({ id: 'chat', createdAt: 0 }),
      sess({ id: 'live', interactive: false, kind: 'trigger', state: 'running', createdAt: 1 }),
      ...Array.from({ length: 14 }, (_, i) => sess({ id: `t${i}`, interactive: false, kind: 'trigger', state: 'done', createdAt: 10 + i })),
    ]
    const out = pruneFinished(list, 12)
    const ids = out.map(s => s.id)
    expect(ids).toContain('chat')
    expect(ids).toContain('live')
    expect(ids).not.toContain('t0')
    expect(ids).not.toContain('t1')
    expect(ids).toContain('t2')
    expect(ids).toContain('t13')
    expect(out.filter(s => s.state === 'done')).toHaveLength(12)
  })

  it('returns the same list when under the cap', () => {
    const list = [sess({ id: 'a', interactive: false, state: 'done' })]
    expect(pruneFinished(list)).toBe(list)
  })
})

describe('nextSelection', () => {
  const list: Session[] = [
    sess({ id: 'chat1' }),
    sess({ id: 'trig', interactive: false, kind: 'trigger' }),
    sess({ id: 'chat2' }),
    sess({ id: 'sched', interactive: false, kind: 'schedule' }),
  ]
  it('prefers the nearest interactive neighbour, left first', () => {
    expect(nextSelection(list, 'chat2')).toBe('chat1')
    expect(nextSelection(list, 'trig')).toBe('chat1')
    expect(nextSelection(list, 'chat1')).toBe('chat2')
  })
  it('falls back to any neighbour, then null', () => {
    const auto: Session[] = [sess({ id: 'a', interactive: false }), sess({ id: 'b', interactive: false })]
    expect(nextSelection(auto, 'b')).toBe('a')
    expect(nextSelection([sess({ id: 'only' })], 'only')).toBeNull()
    expect(nextSelection(list, 'unknown')).toBe('sched')
  })
})

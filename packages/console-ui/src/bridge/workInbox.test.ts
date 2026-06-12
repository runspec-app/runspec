import { describe, it, expect } from 'vitest'
import {
  applyCard, applyConfirm, applyConversation, buildWorkItems, emptyInboxState,
  inboxCounts, removeConfirm, removeConfirmsById, seedRoomState,
} from './workInbox'
import type { PendingConfirm, RoomsState, TriggerCard } from './index'

function confirm(p: Partial<PendingConfirm> = {}): PendingConfirm {
  return { id: 'chat-main', request_id: 'r1', tool_name: 'prod-1__restart', tool_input: {}, autonomy: 'confirm', created_at: 100, ...p }
}
function card(p: Partial<TriggerCard> = {}): TriggerCard {
  return { card_id: 'c1', trigger: 'vip-triage', source: 'outlook', event_type: 'new_mail', summary: 'Outage on web01', column: 'triage', detail: {}, created_at: 50, ...p }
}
const staffed: RoomsState = {
  rooms: [{ room: 'helpdesk/it-support', host: 'web01', user: 'alice@web01', connected: true, queue: 'it-support', threads: {} }],
}

describe('confirm items', () => {
  it('a chat gate is a blocking item routed to the Console tab', () => {
    const s = applyConfirm(emptyInboxState, confirm({ tool_input: { service: 'nginx' } }))
    const [it0] = buildWorkItems(s)
    expect(it0.kind).toBe('confirm')
    expect(it0.urgency).toBe('blocking')
    expect(it0.title).toBe('Approve restart')
    expect(it0.subtitle).toBe('service=nginx')
    expect(it0.origin.tab).toBe('console')
    expect(it0.requestId).toBe('r1')
  })

  it('a gate whose id matches a live trigger card folds into that card (Triggers origin)', () => {
    let s = applyCard(emptyInboxState, card({ card_id: 'card9', column: 'awaiting_human' }))
    s = applyConfirm(s, confirm({ id: 'card9', request_id: 'r9' }))
    const items = buildWorkItems(s)
    // The awaiting_human card is represented by its confirm, not a separate row.
    expect(items).toHaveLength(1)
    expect(items[0].kind).toBe('confirm')
    expect(items[0].title).toBe('vip-triage: restart')
    expect(items[0].origin).toEqual({ tab: 'triggers', select: 'card9' })
  })

  it('a new gate on the same id replaces the prior unresolved one', () => {
    let s = applyConfirm(emptyInboxState, confirm({ request_id: 'r1' }))
    s = applyConfirm(s, confirm({ request_id: 'r2' }))
    expect(Object.keys(s.confirms)).toEqual(['r2'])
  })

  it('run_end drops every gate under that id; resolve drops one', () => {
    let s = applyConfirm(emptyInboxState, confirm({ id: 'chatX', request_id: 'r1' }))
    expect(buildWorkItems(removeConfirmsById(s, 'chatX'))).toHaveLength(0)
    expect(buildWorkItems(removeConfirm(s, 'r1'))).toHaveLength(0)
  })
})

describe('thread items', () => {
  it('an open unclaimed thread in a staffed queue is actionable (claim)', () => {
    let s = seedRoomState(emptyInboxState, staffed)
    s = applyConversation(s, { room: 'helpdesk/it-support/bob@corp', state: 'open', assignee: null })
    const [it0] = buildWorkItems(s)
    expect(it0.kind).toBe('thread')
    expect(it0.urgency).toBe('actionable')
    expect(it0.title).toBe('bob@corp')
    expect(it0.subtitle).toContain('claim')
    expect(it0.room).toBe('helpdesk/it-support/bob@corp')
  })

  it('a thread assigned to us is fyi; assigned to someone else is hidden', () => {
    let s = seedRoomState(emptyInboxState, staffed)
    s = applyConversation(s, { room: 'helpdesk/it-support/bob@corp', state: 'assigned', assignee: 'alice@web01' })
    s = applyConversation(s, { room: 'helpdesk/it-support/eve@corp', state: 'assigned', assignee: 'carol@web01' })
    const items = buildWorkItems(s)
    expect(items).toHaveLength(1)
    expect(items[0].urgency).toBe('fyi')
    expect(items[0].title).toBe('bob@corp')
  })

  it('threads in an unstaffed namespace are ignored', () => {
    const s = applyConversation(emptyInboxState, { room: 'helpdesk/hr/bob', state: 'open', assignee: null })
    expect(buildWorkItems(s)).toHaveLength(0)
  })

  it('resolved / closed threads leave the inbox', () => {
    let s = seedRoomState(emptyInboxState, staffed)
    s = applyConversation(s, { room: 'helpdesk/it-support/bob@corp', state: 'resolved', assignee: 'alice@web01' })
    expect(buildWorkItems(s)).toHaveLength(0)
  })
})

describe('trigger items', () => {
  it('triage / running cards are fyi; done / awaiting are not standalone rows', () => {
    let s = applyCard(emptyInboxState, card({ card_id: 'a', column: 'running' }))
    s = applyCard(s, card({ card_id: 'b', column: 'done' }))
    s = applyCard(s, card({ card_id: 'c', column: 'awaiting_human' }))
    const items = buildWorkItems(s)
    expect(items.map(i => i.cardId)).toEqual(['a'])
    expect(items[0].urgency).toBe('fyi')
  })
})

describe('ordering + counts', () => {
  it('blocking → actionable → fyi, and the badge counts non-fyi', () => {
    let s = seedRoomState(emptyInboxState, staffed)
    s = applyCard(s, card({ card_id: 'c1', column: 'running' }))                       // fyi
    s = applyConversation(s, { room: 'helpdesk/it-support/bob', state: 'open', assignee: null })  // actionable
    s = applyConfirm(s, confirm({ id: 'chat', request_id: 'r1' }))                     // blocking
    const items = buildWorkItems(s)
    expect(items.map(i => i.urgency)).toEqual(['blocking', 'actionable', 'fyi'])
    expect(inboxCounts(items)).toEqual({ count: 2, blocking: 1 })
  })

  it('older blocking gates sort first (closest to timeout)', () => {
    let s = applyConfirm(emptyInboxState, confirm({ id: 'a', request_id: 'r1', created_at: 200 }))
    s = applyConfirm(s, confirm({ id: 'b', request_id: 'r2', created_at: 100 }))
    expect(buildWorkItems(s).map(i => i.requestId)).toEqual(['r2', 'r1'])
  })
})

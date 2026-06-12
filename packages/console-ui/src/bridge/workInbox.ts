// workInbox.ts — the pure projection behind the unified work inbox.
//
// "One list of things needing you" across the three attention surfaces, built
// as an *index* over signals the Triggers, Rooms/helpdesk, and chat confirm-gate
// already emit — not a new store (see docs/design/console-work-inbox.md). This
// module is pure: a small reducer folds snapshots + runspec:* events into an
// InboxState, and buildWorkItems projects that into the ordered WorkItem[] the
// view renders. The React wiring lives in useWorkInbox.ts; keeping the logic
// here means it's unit-testable with no DOM.

import type { PendingConfirm, RoomsState, ThreadState, TriggerCard } from './index'

export type WorkKind = 'confirm' | 'thread' | 'trigger'
// blocking — a parked gate that times out into a deny; surfaces first.
// actionable — wants a decision now (an unclaimed visitor).
// fyi — visible but doesn't nag (your in-progress work).
export type WorkUrgency = 'blocking' | 'actionable' | 'fyi'

export interface WorkItem {
  key: string          // stable: confirm:<request_id> | thread:<room> | trigger:<card_id>
  kind: WorkKind
  title: string
  subtitle?: string
  urgency: WorkUrgency
  ts: number           // seconds; ordering within a tier
  origin: { tab: 'console' | 'triggers' | 'rooms'; select?: string }
  // kind-specific refs the view needs for inline actions / deep-link:
  requestId?: string   // confirm — resolve_tool_confirmation
  room?: string        // thread  — claim_thread / select
  cardId?: string      // trigger — deep-link target
}

// A trigger card, trimmed to the fields the inbox needs.
interface CardLite { card_id: string; trigger: string; summary: string; column: string; created_at: number }

export interface InboxState {
  confirms: Record<string, PendingConfirm>   // by request_id
  cards: Record<string, CardLite>            // by card_id
  threads: Record<string, ThreadState>       // by room
  staff: Record<string, string>              // namespace (helpdesk/<queue>) → our display name
}

export const emptyInboxState: InboxState = { confirms: {}, cards: {}, threads: {}, staff: {} }

// ── reducer: fold one signal into the state (all pure, return new state) ───────

// A new gate for a chat/card replaces any earlier unresolved gate on the same id
// — a turn shows one gate at a time, so an arriving gate means the prior was
// answered (mirrors the App badge's one-pending-per-id assumption).
export function applyConfirm(state: InboxState, c: PendingConfirm): InboxState {
  const confirms: Record<string, PendingConfirm> = {}
  for (const [rid, prev] of Object.entries(state.confirms)) {
    if (prev.id !== c.id) confirms[rid] = prev
  }
  confirms[c.request_id] = c
  return { ...state, confirms }
}

// A turn ended (runspec:run_end {id}) — drop every gate parked under that id.
export function removeConfirmsById(state: InboxState, id: string): InboxState {
  const confirms: Record<string, PendingConfirm> = {}
  for (const [rid, c] of Object.entries(state.confirms)) {
    if (c.id !== id) confirms[rid] = c
  }
  return { ...state, confirms }
}

// Optimistic removal when the user answers a gate from the inbox.
export function removeConfirm(state: InboxState, requestId: string): InboxState {
  if (!(requestId in state.confirms)) return state
  const confirms = { ...state.confirms }
  delete confirms[requestId]
  return { ...state, confirms }
}

export function applyCard(state: InboxState, card: TriggerCard | CardLite): InboxState {
  const lite: CardLite = {
    card_id: card.card_id,
    trigger: card.trigger,
    summary: card.summary,
    column: card.column,
    created_at: card.created_at,
  }
  return { ...state, cards: { ...state.cards, [lite.card_id]: lite } }
}

export function applyConversation(
  state: InboxState,
  frame: { room: string; state: string; assignee: string | null },
): InboxState {
  if (!frame.room) return state
  return {
    ...state,
    threads: { ...state.threads, [frame.room]: { state: frame.state, assignee: frame.assignee } },
  }
}

// Merge a get_room_state snapshot: learn which queues we staff (and our name in
// each) and seed thread states. Merges — never drops live state already learned
// from conversation frames.
export function seedRoomState(state: InboxState, rooms: RoomsState): InboxState {
  const staff = { ...state.staff }
  const threads = { ...state.threads }
  for (const r of rooms.rooms ?? []) {
    if (r.queue) staff[r.room] = r.user   // r.room is the queue namespace (helpdesk/<queue>)
    for (const [room, st] of Object.entries(r.threads ?? {})) threads[room] = st
  }
  return { ...state, staff, threads }
}

// ── projection: state → the ordered list the view renders ─────────────────────

const URGENCY_RANK: Record<WorkUrgency, number> = { blocking: 0, actionable: 1, fyi: 2 }

function displayTool(toolName: string): string {
  return toolName.split('__').pop() || toolName
}

function namespaceOf(room: string): string {
  const parts = room.split('/')
  return parts.length >= 2 ? `${parts[0]}/${parts[1]}` : ''
}

function visitorOf(room: string): string {
  const parts = room.split('/')
  return parts.length >= 3 ? parts.slice(2).join('/') : room
}

export function buildWorkItems(state: InboxState): WorkItem[] {
  const items: WorkItem[] = []

  // Confirms — blocking. A gate whose id matches a live trigger card belongs to
  // that card (origin = Triggers, titled by the trigger); else it's a chat gate.
  for (const c of Object.values(state.confirms)) {
    const card = state.cards[c.id]
    const tool = displayTool(c.tool_name)
    items.push({
      key: `confirm:${c.request_id}`,
      kind: 'confirm',
      title: card ? `${card.trigger}: ${tool}` : `Approve ${tool}`,
      subtitle: summariseInput(c.tool_input),
      urgency: 'blocking',
      ts: c.created_at ?? 0,
      origin: card
        ? { tab: 'triggers', select: card.card_id }
        : { tab: 'console', select: c.id },
      requestId: c.request_id,
      cardId: card ? card.card_id : undefined,
    })
  }

  // Helpdesk threads — actionable when a visitor is waiting unclaimed, fyi when
  // it's one you're already handling. Group chat + others are out of v1 scope.
  for (const [room, st] of Object.entries(state.threads)) {
    const ns = namespaceOf(room)
    const ourName = state.staff[ns]
    if (!ourName || visitorOf(room) === room) continue   // not a staffed conversation
    const visitor = visitorOf(room)
    const queue = ns.split('/')[1] ?? ns
    if (st.state === 'open') {
      items.push({
        key: `thread:${room}`, kind: 'thread',
        title: visitor, subtitle: `waiting in ${queue} — claim`,
        urgency: 'actionable', ts: 0,
        origin: { tab: 'rooms', select: room }, room,
      })
    } else if (st.state === 'assigned' && st.assignee === ourName) {
      items.push({
        key: `thread:${room}`, kind: 'thread',
        title: visitor, subtitle: `you're handling · ${queue}`,
        urgency: 'fyi', ts: 0,
        origin: { tab: 'rooms', select: room }, room,
      })
    }
  }

  // Trigger cards still working (triage / running) — fyi progress. The
  // awaiting_human state is represented by its confirm above, so skip it here.
  for (const card of Object.values(state.cards)) {
    if (card.column !== 'triage' && card.column !== 'running') continue
    items.push({
      key: `trigger:${card.card_id}`, kind: 'trigger',
      title: `${card.trigger}`, subtitle: card.summary,
      urgency: 'fyi', ts: card.created_at,
      origin: { tab: 'triggers', select: card.card_id }, cardId: card.card_id,
    })
  }

  // Tier first; within blocking show the oldest (closest to timeout) first,
  // within actionable/fyi show the newest first.
  return items.sort((a, b) => {
    const r = URGENCY_RANK[a.urgency] - URGENCY_RANK[b.urgency]
    if (r !== 0) return r
    return a.urgency === 'blocking' ? a.ts - b.ts : b.ts - a.ts
  })
}

function summariseInput(input: Record<string, unknown>): string {
  const entries = Object.entries(input ?? {})
  if (entries.length === 0) return ''
  return entries.map(([k, v]) => `${k}=${String(v)}`).join(', ').slice(0, 120)
}

// Count badges: things that actually need an action (blocking + actionable);
// fyi is visible in the list but doesn't drive the badge.
export function inboxCounts(items: WorkItem[]): { count: number; blocking: number } {
  let count = 0
  let blocking = 0
  for (const it of items) {
    if (it.urgency !== 'fyi') count++
    if (it.urgency === 'blocking') blocking++
  }
  return { count, blocking }
}

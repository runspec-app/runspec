/**
 * sessions.ts — the pure model behind the Console's session chip row.
 *
 * Every conversation on the backend — an interactive chat, a trigger fire, a
 * schedule fire — is a *session* with an id; sessions run concurrently. The
 * Console shows one session's blocks at a time (`InvocationBlock.sessionId`).
 * This module is the reducer side: fold `runspec:session` / run-card events
 * into a list, derive a chip's status, prune finished automation sessions,
 * and pick the neighbour to select when one closes. No React, no bridge — the
 * `useSessions` hook wires it to the events; `SessionStrip` renders it.
 *
 * Automation invariant (backend): a trigger / schedule card's `card_id` == its
 * session id == its turn's chat id, so a card *is* the session's identity.
 */
import type { ChatSessionInfo, TriggerCard } from './bridge'

export type SessionKind = ChatSessionInfo['kind']
export type SessionState = ChatSessionInfo['state']

export interface Session {
  id: string
  kind: SessionKind
  label: string
  interactive: boolean
  createdAt: number        // epoch ms
  state: SessionState
  turnId?: string          // chat id of the turn in flight
  unread: boolean          // activity arrived while another session was selected
  // Run-card column for an automation session (awaiting_human / awaiting_claim
  // ⇒ the chip shows "waiting for input"). Absent for interactive sessions.
  column?: string
}

export type SessionStatus = 'running' | 'waiting' | 'idle' | 'done'

/** A `runspec:session` payload: full on create / turn start / turn end, or the
 *  partial `{id, state: 'closed'}` on close / eviction. */
export type SessionPayload = Partial<ChatSessionInfo> & { id: string }

// Card columns that mean "a human has to act before this run continues".
const WAITING_COLUMNS: ReadonlySet<string> = new Set(['awaiting_human', 'awaiting_claim'])
// Terminal card columns.
const TERMINAL_COLUMNS: ReadonlySet<string> = new Set(['done', 'failed'])

// Finished automation sessions kept on the strip (newest first); interactive
// sessions are never pruned.
export const FINISHED_CAP = 12

/** Fold a `runspec:session` event into the list. A `closed` state removes the
 *  session (unknown id ⇒ no-op); a known id merges the changed fields (unread
 *  and card column are UI-owned and survive); an unknown full payload appends. */
export function applySessionEvent(list: Session[], payload: SessionPayload): Session[] {
  if (!payload?.id) return list
  if (payload.state === 'closed') return list.filter(s => s.id !== payload.id)
  const idx = list.findIndex(s => s.id === payload.id)
  if (idx >= 0) {
    const cur = list[idx]
    const next: Session = {
      ...cur,
      ...(payload.kind ? { kind: payload.kind } : {}),
      ...(typeof payload.label === 'string' && payload.label ? { label: payload.label } : {}),
      ...(typeof payload.interactive === 'boolean' ? { interactive: payload.interactive } : {}),
      ...(payload.state ? { state: payload.state } : {}),
      ...(typeof payload.turn === 'string' ? { turnId: payload.turn || undefined } : {}),
    }
    const out = list.slice()
    out[idx] = next
    return out
  }
  // A partial for a session we don't know can't be materialised.
  if (!payload.kind) return list
  return [...list, {
    id: payload.id,
    kind: payload.kind,
    label: payload.label ?? '',
    interactive: payload.interactive ?? payload.kind === 'chat',
    createdAt: (payload.created_at ?? Date.now() / 1000) * 1000,
    state: payload.state ?? 'idle',
    turnId: payload.turn || undefined,
    unread: false,
  }]
}

/** The chip label for a run card. Rule trigger runs read as the target
 *  runnable (`/tool`); agent runs as "Trigger <name> · <event>"; a schedule
 *  fire as "<schedule> · <event_type>". Mirrors the Console block label. */
export function cardLabel(card: TriggerCard, kind: 'trigger' | 'schedule'): string {
  if (kind === 'schedule') return `${card.trigger} · ${card.event_type}`
  const mode = String(card.detail?.mode ?? 'agent')
  if (mode === 'rule') {
    const tool = typeof card.detail?.tool === 'string' ? card.detail.tool : ''
    return tool ? `/${tool}` : card.summary || `Trigger ${card.trigger}`
  }
  return `Trigger ${card.trigger} · ${card.event_type}`
}

/** The read-only session behind a trigger / schedule run card. */
export function sessionFromCard(card: TriggerCard, kind: 'trigger' | 'schedule'): Session {
  return {
    id: card.card_id,
    kind,
    label: cardLabel(card, kind),
    interactive: false,
    createdAt: (card.created_at || Date.now() / 1000) * 1000,
    state: TERMINAL_COLUMNS.has(card.column) ? 'done' : 'running',
    turnId: TERMINAL_COLUMNS.has(card.column) ? undefined : card.card_id,
    unread: false,
    column: card.column,
  }
}

/** Fold a run-card event into the list: first sighting appends the session;
 *  later ones reconcile the column / terminal state (the backend's own label,
 *  when it arrived via `runspec:session`, is kept). */
/** Whether a run card is its own read-only session (chip). True for trigger /
 *  schedule fires; false for a console MCP-server call (`source: "mcp"`),
 *  whose block lands in its client's chat (`card.session`), else — an older
 *  backend — in the operator's current chat. */
export function cardHasOwnSession(card: TriggerCard): boolean {
  return card?.source !== 'mcp'
}

export function applyCardEvent(list: Session[], card: TriggerCard, kind: 'trigger' | 'schedule'): Session[] {
  if (!card?.card_id || !cardHasOwnSession(card)) return list
  const idx = list.findIndex(s => s.id === card.card_id)
  if (idx < 0) return [...list, sessionFromCard(card, kind)]
  const cur = list[idx]
  const terminal = TERMINAL_COLUMNS.has(card.column)
  const next: Session = {
    ...cur,
    column: card.column,
    ...(terminal ? { state: 'done' as const, turnId: undefined } : {}),
  }
  const out = list.slice()
  out[idx] = next
  return out
}

/** What a chip should show. `waiting` = a confirm gate is parked on one of the
 *  session's blocks, or its card is in a column that needs a human. */
export function sessionStatus(
  session: Session,
  blocks: ReadonlyArray<{ id: string; sessionId: string; done: boolean }>,
  pendingConfirmIds: ReadonlySet<string> | ReadonlyArray<string>,
): SessionStatus {
  const pending = pendingConfirmIds instanceof Set ? pendingConfirmIds : new Set(pendingConfirmIds)
  const own = blocks.filter(b => b.sessionId === session.id)
  if (pending.has(session.id) || own.some(b => pending.has(b.id))) return 'waiting'
  if (session.column && WAITING_COLUMNS.has(session.column)) return 'waiting'
  if (session.state === 'running' || own.some(b => !b.done)) return 'running'
  if (session.state === 'done') return 'done'
  return 'idle'
}

export function markUnread(list: Session[], id: string, unread: boolean): Session[] {
  const idx = list.findIndex(s => s.id === id)
  if (idx < 0 || list[idx].unread === unread) return list
  const out = list.slice()
  out[idx] = { ...out[idx], unread }
  return out
}

/** Drop finished automation sessions beyond the newest `cap` (by creation
 *  time). Interactive sessions and running / waiting automation are kept. */
export function pruneFinished(list: Session[], cap = FINISHED_CAP): Session[] {
  const finished = list
    .filter(s => !s.interactive && s.state === 'done')
    .sort((a, b) => b.createdAt - a.createdAt)
  if (finished.length <= cap) return list
  const drop = new Set(finished.slice(cap).map(s => s.id))
  return list.filter(s => !drop.has(s.id))
}

/** Which session to select after `closedId` goes: the nearest interactive
 *  neighbour (left, then right), else the nearest of any kind, else null. */
export function nextSelection(list: Session[], closedId: string): string | null {
  const idx = list.findIndex(s => s.id === closedId)
  const others = list.filter(s => s.id !== closedId)
  if (others.length === 0) return null
  if (idx < 0) return others[others.length - 1].id
  // Position of the closed entry inside `others` (elements after it shifted).
  const at = idx
  const order: Session[] = []
  for (let d = 1; d <= others.length; d++) {
    if (at - d >= 0) order.push(others[at - d])
    if (at - 1 + d < others.length) order.push(others[at - 1 + d])
  }
  const seen = new Set<string>()
  const ranked = order.filter(s => (seen.has(s.id) ? false : (seen.add(s.id), true)))
  return (ranked.find(s => s.interactive) ?? ranked[0]).id
}

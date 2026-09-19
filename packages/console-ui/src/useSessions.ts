/**
 * useSessions — the Console's live session list (see src/sessions.ts).
 *
 * Seeds from the backend (`list_chat_sessions` + both run boards), then folds
 * `runspec:session` / `runspec:trigger_card` / `runspec:schedule_card` into the
 * list; owns the selected session, "New chat", close, the unread dots and the
 * set of blocks parked at a confirm gate (for the chip's "waiting" bell).
 * In-memory only — a restart starts clean (History + resume covers chats).
 *
 * The hook never creates transcript blocks: ConsoleView does that through
 * `onCard` (live + seeded run cards, one reducer) and its own send handlers.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { message } from 'antd'
import { bridge, type ChatSessionInfo, type TriggerCard } from './bridge'
import type { InvocationBlock } from './components/OutputPanel'
import {
  applyCardEvent, applySessionEvent, markUnread, nextSelection, pruneFinished,
  type Session, type SessionPayload,
} from './sessions'

export type CardKind = 'trigger' | 'schedule'

export interface SessionsApi {
  sessions: Session[]
  selectedId: string | null
  selected: Session | null
  // Block ids (chat / card ids) with a confirm gate parked — feeds sessionStatus.
  pendingConfirmIds: Set<string>
  // Select a chip. Calls select_chat_session only for an interactive session;
  // automation sessions are selected locally (they're read-only).
  select: (id: string) => void
  // Open a fresh interactive session and select it (the previous keeps running).
  newChat: () => Promise<string | null>
  // Stop + drop a session (its blocks go via onSessionRemoved).
  close: (id: string) => Promise<void>
  // The interactive session to put new operator work in: the selected one if
  // interactive, else the most recent interactive one (selected), else a new one.
  ensureInteractive: () => Promise<string | null>
  // Register a session the backend reported out of band (a resume result, a
  // clear_chat result) and select it.
  adopt: (info: ChatSessionInfo) => void
}

interface Options {
  // Current transcript blocks — maps a streamed event's id to its session.
  blocksRef: RefObject<InvocationBlock[]>
  // A session left the list (closed / evicted / pruned) — drop its blocks.
  onSessionRemoved?: (id: string) => void
  // A run card arrived (live event or cold-start board) — mirror it as a block.
  onCard?: (card: TriggerCard, kind: CardKind) => void
}

export function useSessions({ blocksRef, onSessionRemoved, onCard }: Options): SessionsApi {
  const [sessions, setSessions] = useState<Session[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [pendingConfirmIds, setPendingConfirmIds] = useState<Set<string>>(() => new Set())
  // Source of truth for the []-dep event handlers (state would be stale there).
  const listRef = useRef<Session[]>([])
  const selectedRef = useRef<string | null>(null)
  const removedRef = useRef(onSessionRemoved)
  removedRef.current = onSessionRemoved
  const cardRef = useRef(onCard)
  cardRef.current = onCard

  const setSelected = useCallback((id: string | null) => {
    selectedRef.current = id
    setSelectedId(id)
  }, [])

  // Every list change goes through here: prune, notify removals, keep the
  // selection valid (a removed selection falls to its neighbour; nothing
  // selected + something listed ⇒ select it, so the strip is never blank
  // while blocks exist).
  const commit = useCallback((fn: (list: Session[]) => Session[]) => {
    const prev = listRef.current
    const next = pruneFinished(fn(prev))
    if (next === prev) return
    const nextIds = new Set(next.map(s => s.id))
    const removed = prev.filter(s => !nextIds.has(s.id)).map(s => s.id)
    listRef.current = next
    setSessions(next)
    for (const id of removed) removedRef.current?.(id)
    const sel = selectedRef.current
    if (sel && !nextIds.has(sel)) {
      const cand = nextSelection(prev, sel)
      const fallback = next.length ? next[next.length - 1].id : null
      setSelected(cand && nextIds.has(cand) ? cand : fallback)
    } else if (!sel && next.length) {
      const lastInteractive = [...next].reverse().find(s => s.interactive)
      setSelected((lastInteractive ?? next[next.length - 1]).id)
    }
  }, [setSelected])

  // Cold start: every live session + both run boards (a fire that happened
  // before this view mounted). Defensive against an older backend / a stubbed
  // bridge returning nothing.
  useEffect(() => {
    let cancelled = false
    const safe = async <T,>(call: () => Promise<T> | undefined): Promise<T | undefined> => {
      try { return await call() } catch { return undefined }
    }
    ;(async () => {
      const [chats, triggers, schedules] = await Promise.all([
        safe(() => bridge.list_chat_sessions()),
        safe(() => bridge.get_trigger_board()),
        safe(() => bridge.get_schedule_board()),
      ])
      if (cancelled) return
      const chatList = Array.isArray(chats) ? chats : []
      const cards: Array<[TriggerCard, CardKind]> = [
        ...(Array.isArray(triggers) ? triggers : []).map(c => [c, 'trigger'] as [TriggerCard, CardKind]),
        ...(Array.isArray(schedules) ? schedules : []).map(c => [c, 'schedule'] as [TriggerCard, CardKind]),
      ]
      commit(list => {
        let n = list
        for (const c of chatList) n = applySessionEvent(n, c)
        for (const [c, kind] of cards) n = applyCardEvent(n, c, kind)
        // Oldest first, whatever order the three feeds came in.
        return n.slice().sort((a, b) => a.createdAt - b.createdAt)
      })
      for (const [c, kind] of cards) cardRef.current?.(c, kind)
    })()
    return () => { cancelled = true }
  }, [commit])

  // Live events.
  useEffect(() => {
    const onSession = (e: Event) => {
      const p = (e as CustomEvent).detail as SessionPayload
      commit(list => applySessionEvent(list, p))
    }
    const onTriggerCard = (e: Event) => {
      const c = (e as CustomEvent).detail as TriggerCard
      commit(list => applyCardEvent(list, c, 'trigger'))
      cardRef.current?.(c, 'trigger')
    }
    const onScheduleCard = (e: Event) => {
      const c = (e as CustomEvent).detail as TriggerCard
      commit(list => applyCardEvent(list, c, 'schedule'))
      cardRef.current?.(c, 'schedule')
    }
    // AI-off "Clear": the backend swapped `replaced` for `session` (an id).
    const onCleared = (e: Event) => {
      const { session, replaced } = (e as CustomEvent).detail as { session?: unknown; replaced?: string }
      commit(list => {
        let n = replaced ? applySessionEvent(list, { id: replaced, state: 'closed' }) : list
        if (session && typeof session === 'object') n = applySessionEvent(n, session as SessionPayload)
        else if (typeof session === 'string' && session && !n.some(s => s.id === session)) {
          n = applySessionEvent(n, { id: session, kind: 'chat', label: '', interactive: true, state: 'idle', turn: '', turns: 0, created_at: Date.now() / 1000 })
        }
        return n
      })
      if (typeof session === 'string' && session) setSelected(session)
      else if (session && typeof session === 'object' && (session as { id?: string }).id) setSelected((session as { id: string }).id)
    }
    // Which session a streamed event belongs to: its block's session, else the
    // event id itself when that is a session (automation: card id == chat id),
    // else the `session` the backend stamped on run_end.
    const sessionOf = (id: string, session?: string): string | null => {
      const blk = blocksRef.current?.find(b => b.id === id)
      if (blk) return blk.sessionId
      if (listRef.current.some(s => s.id === id)) return id
      return session ?? null
    }
    const noteActivity = (id: string, session?: string) => {
      const sid = sessionOf(id, session)
      if (!sid || sid === selectedRef.current) return
      commit(list => markUnread(list, sid, true))
    }
    const onToken = (e: Event) => {
      const { id } = (e as CustomEvent).detail as { id: string }
      noteActivity(id)
    }
    const onConfirm = (e: Event) => {
      const { id } = (e as CustomEvent).detail as { id: string }
      setPendingConfirmIds(prev => (prev.has(id) ? prev : new Set(prev).add(id)))
      noteActivity(id)
    }
    const onResolved = (e: Event) => {
      const { id } = (e as CustomEvent).detail as { id: string }
      setPendingConfirmIds(prev => { if (!prev.has(id)) return prev; const n = new Set(prev); n.delete(id); return n })
    }
    const onEnd = (e: Event) => {
      const { id, session } = (e as CustomEvent).detail as { id: string; session?: string }
      setPendingConfirmIds(prev => { if (!prev.has(id)) return prev; const n = new Set(prev); n.delete(id); return n })
      noteActivity(id, session)
    }
    window.addEventListener('runspec:session', onSession)
    window.addEventListener('runspec:trigger_card', onTriggerCard)
    window.addEventListener('runspec:schedule_card', onScheduleCard)
    window.addEventListener('runspec:chat_cleared', onCleared)
    window.addEventListener('runspec:token', onToken)
    window.addEventListener('runspec:tool_confirm', onConfirm)
    window.addEventListener('runspec:confirm_resolved', onResolved)
    window.addEventListener('runspec:run_end', onEnd)
    return () => {
      window.removeEventListener('runspec:session', onSession)
      window.removeEventListener('runspec:trigger_card', onTriggerCard)
      window.removeEventListener('runspec:schedule_card', onScheduleCard)
      window.removeEventListener('runspec:chat_cleared', onCleared)
      window.removeEventListener('runspec:token', onToken)
      window.removeEventListener('runspec:tool_confirm', onConfirm)
      window.removeEventListener('runspec:confirm_resolved', onResolved)
      window.removeEventListener('runspec:run_end', onEnd)
    }
  }, [blocksRef, commit, setSelected])

  const select = useCallback((id: string) => {
    const s = listRef.current.find(x => x.id === id)
    if (!s) return
    setSelected(id)
    commit(list => markUnread(list, id, false))
    // Best-effort: an older backend without the method must not break selection.
    if (s.interactive) void Promise.resolve().then(() => bridge.select_chat_session(id)).catch(() => {})
  }, [commit, setSelected])

  const adopt = useCallback((info: ChatSessionInfo) => {
    if (!info?.id) return
    commit(list => applySessionEvent(list, info))
    setSelected(info.id)
  }, [commit, setSelected])

  const newChat = useCallback(async (): Promise<string | null> => {
    try {
      const info = await bridge.new_chat_session()
      if (!info?.id) return null
      adopt(info)
      return info.id
    } catch {
      message.error('Could not open a new chat')
      return null
    }
  }, [adopt])

  const close = useCallback(async (id: string) => {
    try { await bridge.close_chat_session(id) } catch { /* dropped locally regardless */ }
    commit(list => list.filter(s => s.id !== id))
  }, [commit])

  const ensureInteractive = useCallback(async (): Promise<string | null> => {
    const sel = listRef.current.find(s => s.id === selectedRef.current)
    if (sel?.interactive) return sel.id
    const last = [...listRef.current].reverse().find(s => s.interactive)
    if (last) { select(last.id); return last.id }
    return newChat()
  }, [newChat, select])

  const selected = useMemo(() => sessions.find(s => s.id === selectedId) ?? null, [sessions, selectedId])

  return { sessions, selectedId, selected, pendingConfirmIds, select, newChat, close, ensureInteractive, adopt }
}

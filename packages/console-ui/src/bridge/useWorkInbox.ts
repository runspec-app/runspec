// useWorkInbox.ts — React wiring for the work-inbox projection (workInbox.ts).
//
// Lives at the App root (like the triggerAwaiting / roomAttention effects), NOT
// inside the inbox view: the Triggers/Rooms views are mounted-always so their
// state survives tab switches, and the inbox projection needs the same lifetime
// so it keeps accruing while you're on another tab. Seeds from the three
// snapshots, then folds the live runspec:* events into the pure reducer.

import { useCallback, useEffect, useMemo, useState } from 'react'
import { bridge, type PendingConfirm, type TriggerCard } from './index'
import {
  applyCard, applyConfirm, applyConversation, buildWorkItems, emptyInboxState,
  inboxCounts, removeConfirm, removeConfirmsById, seedRoomState, type InboxState,
} from './workInbox'

export function useWorkInbox() {
  const [state, setState] = useState<InboxState>(emptyInboxState)

  useEffect(() => {
    let cancelled = false

    // Cold-start: a gate / thread can already exist before we subscribed.
    Promise.all([
      bridge.get_pending_confirms().catch(() => [] as PendingConfirm[]),
      bridge.get_trigger_board().catch(() => [] as TriggerCard[]),
      bridge.get_room_state().catch(() => ({ rooms: [] })),
    ]).then(([confirms, board, rooms]) => {
      if (cancelled) return
      setState(s => {
        let n = seedRoomState(s, rooms)
        for (const c of board) n = applyCard(n, c)
        for (const c of confirms) n = applyConfirm(n, c)
        return n
      })
    })

    const onConfirm = (e: Event) =>
      setState(s => applyConfirm(s, (e as CustomEvent<PendingConfirm>).detail))
    const onRunEnd = (e: Event) =>
      setState(s => removeConfirmsById(s, (e as CustomEvent<{ id: string }>).detail.id))
    const onCard = (e: Event) =>
      setState(s => applyCard(s, (e as CustomEvent<TriggerCard>).detail))
    const onConversation = (e: Event) =>
      setState(s => applyConversation(s, (e as CustomEvent<{ room: string; state: string; assignee: string | null }>).detail))
    // A room/queue (dis)connecting can add staffed namespaces — re-seed.
    const onConnection = () =>
      bridge.get_room_state().then(rs => { if (!cancelled) setState(s => seedRoomState(s, rs)) }).catch(() => {})

    window.addEventListener('runspec:tool_confirm', onConfirm)
    window.addEventListener('runspec:run_end', onRunEnd)
    window.addEventListener('runspec:trigger_card', onCard)
    window.addEventListener('runspec:room_conversation', onConversation)
    window.addEventListener('runspec:room_connection', onConnection)
    return () => {
      cancelled = true
      window.removeEventListener('runspec:tool_confirm', onConfirm)
      window.removeEventListener('runspec:run_end', onRunEnd)
      window.removeEventListener('runspec:trigger_card', onCard)
      window.removeEventListener('runspec:room_conversation', onConversation)
      window.removeEventListener('runspec:room_connection', onConnection)
    }
  }, [])

  const items = useMemo(() => buildWorkItems(state), [state])
  const counts = useMemo(() => inboxCounts(items), [items])

  // Inline actions route back to the owning bridge methods; the owner's terminal
  // event (run_end / conversation frame) removes the item. Confirms also drop
  // optimistically so the row clears instantly.
  const resolveConfirm = useCallback((requestId: string, approved: boolean) => {
    setState(s => removeConfirm(s, requestId))
    bridge.resolve_tool_confirmation(requestId, approved).catch(() => {})
  }, [])
  const claimThread = useCallback((room: string) => {
    bridge.claim_thread(room).catch(() => {})
  }, [])

  return { items, count: counts.count, blocking: counts.blocking, resolveConfirm, claimThread }
}

export type WorkInbox = ReturnType<typeof useWorkInbox>

/**
 * Acknowledged failures of this console's own schedules: schedule id → the
 * failed run (card id) the operator has seen. Kept on this machine, like the
 * schedules themselves; host schedules use the shared claim / dealt-with notes
 * instead, since other people see those.
 */
import { useCallback, useState } from 'react'

export const SCHEDULE_ACKS_KEY = 'rs.scheduleAcks'

export type ScheduleAcks = Record<string, string>

export function loadAcks(storage: Pick<Storage, 'getItem'> | null = safeStorage()): ScheduleAcks {
  try {
    const v = JSON.parse(storage?.getItem(SCHEDULE_ACKS_KEY) || '{}')
    if (!v || typeof v !== 'object' || Array.isArray(v)) return {}
    return Object.fromEntries(Object.entries(v).filter(([, c]) => typeof c === 'string')) as ScheduleAcks
  } catch {
    return {}
  }
}

export function saveAcks(acks: ScheduleAcks, storage: Pick<Storage, 'setItem'> | null = safeStorage()): void {
  try { storage?.setItem(SCHEDULE_ACKS_KEY, JSON.stringify(acks)) } catch { /* storage unavailable */ }
}

/** Acknowledge a schedule's failed run (`cardId`), or clear it with `null`. */
export function setAck(acks: ScheduleAcks, scheduleId: string, cardId: string | null): ScheduleAcks {
  const next = { ...acks }
  if (cardId) next[scheduleId] = cardId
  else delete next[scheduleId]
  return next
}

/** The acknowledgements, and a setter that also saves them. */
export function useScheduleAcks(): [ScheduleAcks, (scheduleId: string, cardId: string | null) => void] {
  const [acks, setAcks] = useState<ScheduleAcks>(() => loadAcks())
  const update = useCallback((scheduleId: string, cardId: string | null) => {
    setAcks(prev => {
      const next = setAck(prev, scheduleId, cardId)
      saveAcks(next)
      return next
    })
  }, [])
  return [acks, update]
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null
  } catch {
    return null
  }
}

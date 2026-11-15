/**
 * The date range History → Runs and History → Charts share, so switching
 * between them never shows two windows whose totals seem to disagree (Runs
 * opened on 7 days, Charts on 30). In memory for the session.
 */
import { useSyncExternalStore } from 'react'

let current = 7
const listeners = new Set<() => void>()

export function setHistoryRange(days: number): void {
  if (days === current) return
  current = days
  listeners.forEach(f => f())
}

export function useHistoryRange(): [number, (days: number) => void] {
  const days = useSyncExternalStore(
    cb => { listeners.add(cb); return () => { listeners.delete(cb) } },
    () => current,
  )
  return [days, setHistoryRange]
}

/** Charts read the history cache, which keeps 30 days: wider (or All) is 30. */
export function chartDays(days: number): number {
  return days === 0 || days > 30 ? 30 : days
}

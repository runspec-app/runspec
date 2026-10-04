/**
 * useWaitingCue — keeps the looping "waiting on you" ping going while something
 * in the console needs the operator, and stops it the moment that clears.
 *
 * "Needs the operator" = a tab carries an attention badge: the Triggers count
 * (pending trigger approvals + unclaimed queue callers, `triggerAwaiting`) or
 * any live Approve/Deny card anywhere (a chat or MCP-server confirm — the
 * amber bell on a session chip). Progress badges (runs in flight, the Triggers
 * "new activity" dot) don't count: nothing is waiting to be resolved there.
 *
 * Playback is backend-side (`set_waiting_cue`), because a minimised WebView2
 * throttles in-page audio and timers. While active this re-asserts every
 * WAITING_REASSERT_MS; the backend lets the loop lapse if those stop, so a
 * reload mid-wait can't leave it pinging forever.
 */
import { useEffect, useState } from 'react'
import { bridge } from './bridge'

export const WAITING_REASSERT_MS = 15_000

/** Whether the waiting ping should be playing. Pure, for tests. */
export function waitingCueActive(triggerAwaiting: number, pendingConfirms: number): boolean {
  return triggerAwaiting > 0 || pendingConfirms > 0
}

export function useWaitingCue(triggerAwaiting: number): boolean {
  // Live Approve/Deny cards, by tool-call id — the same lifecycle the session
  // chips follow (tool_confirm opens, confirm_resolved / run_end closes).
  const [pending, setPending] = useState<ReadonlySet<string>>(() => new Set())
  useEffect(() => {
    const add = (e: Event) => {
      const { id } = ((e as CustomEvent).detail ?? {}) as { id?: string }
      if (id) setPending(prev => (prev.has(id) ? prev : new Set(prev).add(id)))
    }
    const drop = (e: Event) => {
      const { id } = ((e as CustomEvent).detail ?? {}) as { id?: string }
      if (id) setPending(prev => { if (!prev.has(id)) return prev; const n = new Set(prev); n.delete(id); return n })
    }
    window.addEventListener('runspec:tool_confirm', add)
    window.addEventListener('runspec:confirm_resolved', drop)
    window.addEventListener('runspec:run_end', drop)
    return () => {
      window.removeEventListener('runspec:tool_confirm', add)
      window.removeEventListener('runspec:confirm_resolved', drop)
      window.removeEventListener('runspec:run_end', drop)
    }
  }, [])

  const active = waitingCueActive(triggerAwaiting, pending.size)
  useEffect(() => {
    const send = (on: boolean) => { Promise.resolve(bridge.set_waiting_cue?.(on)).catch(() => {}) }
    if (!active) { send(false); return }
    send(true)
    const t = window.setInterval(() => send(true), WAITING_REASSERT_MS)
    return () => { window.clearInterval(t); send(false) }
  }, [active])
  return active
}

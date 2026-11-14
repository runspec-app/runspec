/**
 * Webhooks the console follows on its hosts (runspec-scheduler `hooks follow`).
 *
 * `useWebhooks()` is the single source for the approval cards, the status bar
 * count and the Schedules tab's webhook strips: it reads `webhooks_status` once
 * and then keeps it current from `runspec:webhooks` pushes ({venv} replaces one
 * followed venv; {all: true} refetches everything).
 *
 * The rest of this file is pure, for tests.
 */
import { useCallback, useEffect, useState } from 'react'
import { bridge } from './bridge'
import type { WebhookDelivery, WebhookVenv, WebhooksStatus } from './bridge'
import type { StatusTone } from './components/kit'

export const EMPTY_WEBHOOKS: WebhooksStatus = { configured: false, away: '', venvs: [], follows: {} }

/** Deliveries a person still has to decide, oldest first, with their venv. */
export interface WaitingDelivery { venv: WebhookVenv; delivery: WebhookDelivery }

export function waitingApprovals(status: WebhooksStatus): WaitingDelivery[] {
  const out: WaitingDelivery[] = []
  for (const venv of status.venvs) {
    for (const delivery of venv.open) {
      if (delivery.state === 'pending') out.push({ venv, delivery })
    }
  }
  return out.sort((a, b) => (a.delivery.received ?? '').localeCompare(b.delivery.received ?? ''))
}

/** Deliveries someone already approved that haven't finished: shown on the
 *  console as "approved by …" until they settle. */
export function inFlight(status: WebhooksStatus): WaitingDelivery[] {
  const out: WaitingDelivery[] = []
  for (const venv of status.venvs) {
    for (const delivery of venv.open) {
      if (delivery.decided_by && delivery.state !== 'pending') out.push({ venv, delivery })
    }
  }
  return out
}

/** One followed venv's connection, as a word and a tone. */
export function connection(v: WebhookVenv): { label: string; tone: StatusTone; detail: string } {
  if (v.unavailable) return { label: 'not available', tone: 'neutral', detail: v.unavailable }
  if (v.paused) return { label: 'paused', tone: 'attention', detail: v.paused }
  if (v.connected) return { label: 'following', tone: 'ok', detail: '' }
  if (v.error) return { label: 'not connected', tone: 'failed', detail: v.error }
  return { label: 'connecting', tone: 'running', detail: '' }
}

const STATE_TONE: Record<string, StatusTone> = {
  pending: 'waiting', queued: 'running', approved: 'running', waiting: 'running', running: 'running',
  denied: 'neutral', skipped: 'neutral', event: 'info', 'no-match': 'neutral', duplicate: 'neutral',
  expired: 'attention', refused: 'attention', error: 'failed',
}

/** A delivery's state as a tag: a run that exited non-zero reads as failed. */
export function deliveryStatus(d: WebhookDelivery): { label: string; tone: StatusTone } {
  if (d.state === 'ran') {
    return d.exit_code ? { label: `failed (exit ${d.exit_code})`, tone: 'failed' } : { label: 'ran', tone: 'ok' }
  }
  if (d.state === 'pending') return { label: 'waiting for approval', tone: 'waiting' }
  if (d.state === 'event') return { label: 'event only', tone: 'info' }
  return { label: d.state, tone: STATE_TONE[d.state] ?? 'neutral' }
}

function argText(v: unknown): string {
  const s = typeof v === 'string' ? v : JSON.stringify(v)
  return /^[\w./:@%+=,-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`
}

/** What a delivery would run, as a command line: `system-service restart --name nginx`. */
export function runLine(d: { runnable?: string; command?: string[]; args?: Record<string, unknown> }): string {
  if (!d.runnable) return ''
  const parts = [d.runnable, ...(d.command ?? [])]
  for (const [k, v] of Object.entries(d.args ?? {})) {
    if (v === true) parts.push(`--${k}`)
    else if (v === false || v == null || v === '') continue
    else if (Array.isArray(v)) v.forEach(x => parts.push(`--${k}`, argText(x)))
    else parts.push(`--${k}`, argText(v))
  }
  return parts.join(' ')
}

/** A delivery's fields one level deep (`labels.severity`), for a short list. */
export function flatFields(fields: Record<string, unknown>, prefix = '', out: [string, string][] = []): [string, string][] {
  for (const [k, v] of Object.entries(fields ?? {})) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object' && !Array.isArray(v) && out.length < 200) flatFields(v as Record<string, unknown>, key, out)
    else out.push([key, typeof v === 'string' ? v : JSON.stringify(v)])
  }
  return out
}

/** "3m ago" / "in 12m" from an ISO time; '' when there's none. */
export function relTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return ''
  const s = Math.round((now - t) / 1000)
  const a = Math.abs(s)
  const span = a < 60 ? `${a}s` : a < 3600 ? `${Math.round(a / 60)}m` : a < 86400 ? `${Math.round(a / 3600)}h` : `${Math.round(a / 86400)}d`
  if (a < 5) return 'just now'
  return s >= 0 ? `${span} ago` : `in ${span}`
}

/** A mute's target in words. */
export function muteLabel(hook: string): string {
  return hook === '*' ? 'all hooks' : hook
}

/** The hosts a status bar click / a Follow offer cares about: daemon venvs
 *  whose host doesn't follow any hooks yet. */
export function unfollowedHosts(status: WebhooksStatus, daemonHosts: string[]): string[] {
  return [...new Set(daemonHosts)].filter(h => !(status.follows[h]?.length)).sort()
}

/** Apply a `runspec:webhooks` push to the current status. */
export function applyPush(status: WebhooksStatus, detail: { venv?: WebhookVenv; all?: boolean }): WebhooksStatus | null {
  if (detail?.all) return null // refetch
  const v = detail?.venv
  if (!v || !Array.isArray(v.open)) return status
  const venvs = status.venvs.some(x => x.key === v.key)
    ? status.venvs.map(x => (x.key === v.key ? v : x))
    : [...status.venvs, v].sort((a, b) => a.key.localeCompare(b.key))
  return { ...status, venvs, away: v.away ?? status.away }
}

export function useWebhooks(): { status: WebhooksStatus; refresh: () => void } {
  const [status, setStatus] = useState<WebhooksStatus>(EMPTY_WEBHOOKS)
  const refresh = useCallback(() => {
    Promise.resolve(bridge.webhooks_status?.())
      .then(s => { if (s && Array.isArray(s.venvs)) setStatus({ ...EMPTY_WEBHOOKS, ...s }) })
      .catch(() => {})
  }, [])
  useEffect(() => {
    refresh()
    const onPush = (e: Event) => {
      const detail = ((e as CustomEvent).detail ?? {}) as { venv?: WebhookVenv; all?: boolean }
      if (detail.all) { refresh(); return }
      setStatus(prev => applyPush(prev, detail) ?? prev)
    }
    window.addEventListener('runspec:webhooks', onPush)
    return () => window.removeEventListener('runspec:webhooks', onPush)
  }, [refresh])
  return { status, refresh }
}

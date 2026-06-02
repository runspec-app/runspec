/**
 * devbus.ts — in-session activity recorder for the Dev tab.
 *
 * Captures the app's entire bidirectional activity stream from two choke points:
 *   - frontend → backend bridge calls (the Proxy in bridge/index.ts)
 *   - backend → frontend `runspec:*` events (window.dispatchEvent, tapped here)
 *
 * Everything lives in a bounded in-memory ring buffer that resets each launch —
 * no disk persistence. The buffer is exposed via the `useSyncExternalStore`
 * contract (`subscribe` + `getSnapshot`); `getSnapshot` returns a reference that
 * changes ONLY on mutation, so React re-renders exactly when the data changes
 * (and never loops).
 */

export type DevDirection = 'call' | 'event'

export type DevCategory =
  | 'bridge-call'
  | 'output'
  | 'token'
  | 'tool'
  | 'run_end'
  | 'usage'
  | 'discovery'
  | 'event'
  | 'error'

export interface DevEntry {
  id: number // monotonic sequence number — also the React key
  ts: number // Date.now() at record time
  direction: DevDirection
  category: DevCategory
  name: string // method name ("get_hosts") or event type ("runspec:token")
  summary: string // one-line human summary
  durationMs?: number // bridge calls only, set on settle
  status: 'pending' | 'ok' | 'error'
  error?: string
  detail?: unknown // full args / result / event detail (live reference)
  count?: number // >1 when this entry coalesces repeated token/output events
}

export interface DevStats {
  calls: number
  events: number
  errors: number
  total: number
}

// Tunable: how many entries the ring buffer retains. On overflow the oldest
// entries are dropped from the front.
const CAP = 1000

// Cap on the appended snippet length for coalesced token/output entries, so a
// long stream doesn't grow one entry's detail without bound.
const COALESCE_SNIPPET_CAP = 200

let buf: DevEntry[] = []
let seq = 0
let paused = false

const listeners = new Set<() => void>()

// Running totals, kept incrementally so DevView never rescans the whole buffer.
// Reflect the *lifetime* of the current session, not just what's still resident
// in the (capped) buffer — these are counters, not buffer-size gauges.
let statCalls = 0
let statEvents = 0
let statErrors = 0
let stats: DevStats = { calls: 0, events: 0, errors: 0, total: 0 }

function notify(): void {
  for (const l of listeners) l()
}

function refreshStats(): void {
  stats = { calls: statCalls, events: statEvents, errors: statErrors, total: buf.length }
}

/** Append an entry, evicting from the front past CAP. Replaces `buf` (new ref). */
function push(entry: DevEntry): void {
  const next = buf.length >= CAP ? buf.slice(buf.length - CAP + 1) : buf.slice()
  next.push(entry)
  buf = next
  refreshStats()
  notify()
}

/** Replace an existing entry in place (by id) with a resolved version. */
function replace(id: number, patch: Partial<DevEntry>): void {
  const idx = buf.findIndex(e => e.id === id)
  if (idx === -1) return
  const next = buf.slice()
  next[idx] = { ...next[idx], ...patch }
  buf = next
  refreshStats()
  notify()
}

// ── useSyncExternalStore contract ──────────────────────────────────────────

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getSnapshot(): DevEntry[] {
  return buf
}

export function getStats(): DevStats {
  return stats
}

// ── controls ───────────────────────────────────────────────────────────────

export function clear(): void {
  buf = []
  refreshStats()
  notify()
}

export function setPaused(p: boolean): void {
  paused = p
}

export function isPaused(): boolean {
  return paused
}

// ── bridge-call recording (FE → BE) ─────────────────────────────────────────

export function recordCallStart(name: string, args: unknown[]): number {
  const id = ++seq
  if (paused) return id
  statCalls++
  push({
    id,
    ts: Date.now(),
    direction: 'call',
    category: 'bridge-call',
    name,
    summary: summarizeArgs(args),
    status: 'pending',
    detail: args,
  })
  return id
}

export function recordCallEnd(id: number, result: unknown, durationMs: number): void {
  if (paused) return
  replace(id, {
    status: 'ok',
    durationMs,
    detail: { args: detailArgsOf(id), result },
  })
}

export function recordCallError(id: number, error: unknown, durationMs: number): void {
  if (paused) return
  // Bump the counter before replace() — replace() calls refreshStats()/notify(),
  // so the new error count must already be in place when subscribers re-render.
  statErrors++
  replace(id, {
    status: 'error',
    category: 'error',
    durationMs,
    error: errorMessage(error),
    detail: { args: detailArgsOf(id), error },
  })
}

function detailArgsOf(id: number): unknown {
  const e = buf.find(x => x.id === id)
  if (!e) return undefined
  // Start entries store the raw args array as `detail`; resolved entries wrap it.
  const d = e.detail as { args?: unknown } | unknown[]
  return Array.isArray(d) ? d : (d as { args?: unknown })?.args
}

// ── event recording (BE → FE and inter-component) ────────────────────────────

export function recordEvent(type: string, detail: unknown): void {
  if (paused) return
  const category = categoryOf(type)

  // Coalesce high-frequency streams (per-token chat, per-line output) so a long
  // stream collapses to a single entry with a count badge instead of flooding
  // the buffer. Only coalesce onto the *most recent* entry of the same key.
  const key = coalesceKey(type, detail)
  if (key) {
    const last = buf[buf.length - 1]
    if (last && last.category === category && (last as DevEntry & { _ckey?: string })._ckey === key) {
      const next = buf.slice()
      const updated: DevEntry & { _ckey?: string } = {
        ...last,
        count: (last.count ?? 1) + 1,
        summary: appendSnippet(last.summary, type, detail),
        detail,
      }
      next[next.length - 1] = updated
      buf = next
      statEvents++
      refreshStats()
      notify()
      return
    }
  }

  statEvents++
  const entry: DevEntry & { _ckey?: string } = {
    id: ++seq,
    ts: Date.now(),
    direction: 'event',
    category,
    name: type,
    summary: summarizeEvent(type, detail),
    status: 'ok',
    detail,
    count: key ? 1 : undefined,
  }
  if (key) entry._ckey = key
  push(entry)
}

function categoryOf(type: string): DevCategory {
  if (type === 'runspec:output') return 'output'
  if (type === 'runspec:token') return 'token'
  if (type.startsWith('runspec:tool')) return 'tool'
  if (type === 'runspec:run_end') return 'run_end'
  if (type === 'runspec:chat_usage') return 'usage'
  if (type === 'runspec:hosts_updated' || type === 'runspec:runnables_updated') return 'discovery'
  return 'event'
}

function coalesceKey(type: string, detail: unknown): string | null {
  const d = detail as Record<string, unknown> | undefined
  const id = d?.id
  if (type === 'runspec:token') return `token:${id ?? '?'}`
  if (type === 'runspec:output') return `output:${id ?? '?'}:${d?.stream ?? '?'}`
  return null
}

function appendSnippet(prev: string, type: string, detail: unknown): string {
  const d = detail as Record<string, unknown> | undefined
  const piece = type === 'runspec:token' ? String(d?.token ?? '') : String(d?.line ?? '')
  const merged = prev + piece
  return merged.length > COALESCE_SNIPPET_CAP
    ? '…' + merged.slice(merged.length - COALESCE_SNIPPET_CAP)
    : merged
}

function summarizeEvent(type: string, detail: unknown): string {
  const d = (detail ?? {}) as Record<string, unknown>
  switch (type) {
    case 'runspec:output':
      return `[${d.stream ?? 'stdout'}] ${truncate(String(d.line ?? ''), 120)}`
    case 'runspec:token':
      return truncate(String(d.token ?? ''), 120)
    case 'runspec:tool_start':
      return `▶ ${d.tool_name ?? '?'}`
    case 'runspec:tool_end':
      return `■ ${d.tool_name ?? '?'}`
    case 'runspec:tool_confirm':
      return `confirm ${d.tool_name ?? d.runnable ?? ''}`.trim()
    case 'runspec:run_end':
      return `exit ${d.exit_code ?? '?'} in ${d.duration_ms ?? '?'}ms`
    case 'runspec:chat_usage':
      return `in ${d.input_tokens ?? 0} · out ${d.output_tokens ?? 0} · cache_read ${d.cache_read_tokens ?? 0}`
    case 'runspec:hosts_updated':
      return 'hosts refreshed'
    case 'runspec:runnables_updated':
      return 'runnables refreshed'
    default: {
      // Surface the most identifying field if present.
      const hint = d.id ?? d.message ?? d.runnable ?? d.host ?? ''
      return hint ? `${truncate(String(hint), 120)}` : ''
    }
  }
}

// ── dispatchEvent tap (BE → FE + inter-component) ────────────────────────────

let tapInstalled = false

/**
 * Patch window.dispatchEvent ONCE so every `runspec:*` CustomEvent is tee'd into
 * the buffer. Record-only (never re-dispatches → no recursion); non-`runspec:`
 * events are ignored; the original dispatch is always called exactly once with
 * its return value preserved. Idempotent + safe under React StrictMode.
 */
export function installDispatchTap(): void {
  if (tapInstalled) return
  if (typeof window === 'undefined') return
  tapInstalled = true
  const orig = window.dispatchEvent.bind(window)
  window.dispatchEvent = (event: Event): boolean => {
    try {
      if (event instanceof CustomEvent && event.type.startsWith('runspec:')) {
        recordEvent(event.type, event.detail)
      }
    } catch {
      // Never let bookkeeping break real event delivery.
    }
    return orig(event)
  }
}

// Exposed for tests so each case starts from a clean slate.
export function _resetForTests(): void {
  buf = []
  seq = 0
  paused = false
  statCalls = 0
  statEvents = 0
  statErrors = 0
  tapInstalled = false
  refreshStats()
  listeners.clear()
}

// ── helpers ──────────────────────────────────────────────────────────────────

function summarizeArgs(args: unknown[]): string {
  if (!args || args.length === 0) return ''
  return truncate(args.map(formatArg).join(', '), 120)
}

function formatArg(a: unknown): string {
  if (a === null) return 'null'
  if (a === undefined) return 'undefined'
  if (typeof a === 'string') return JSON.stringify(truncate(a, 40))
  if (typeof a === 'number' || typeof a === 'boolean') return String(a)
  if (Array.isArray(a)) return `[${a.length}]`
  return '{…}'
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'string') return error
  try {
    return JSON.stringify(error)
  } catch {
    return String(error)
  }
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n) + '…' : s
}

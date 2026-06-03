import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import {
  subscribe,
  getSnapshot,
  getStats,
  clear,
  setPaused,
  recordCallStart,
  recordCallEnd,
  recordCallError,
  recordEvent,
  installDispatchTap,
  _resetForTests,
} from './devbus'

beforeEach(() => {
  _resetForTests()
})

describe('ring buffer', () => {
  it('appends entries and tracks buffer size', () => {
    recordEvent('runspec:run_end', { id: 'a', exit_code: 0, duration_ms: 5 })
    recordEvent('runspec:run_end', { id: 'b', exit_code: 1, duration_ms: 9 })
    expect(getSnapshot()).toHaveLength(2)
    expect(getStats().total).toBe(2)
  })

  it('evicts oldest entries past CAP', () => {
    // CAP is 1000; push more and confirm the buffer caps and drops the front.
    for (let i = 0; i < 1100; i++) {
      recordEvent('runspec:run_end', { id: `r${i}`, exit_code: 0, duration_ms: 1 })
    }
    const snap = getSnapshot()
    expect(snap).toHaveLength(1000)
    // The first survivors should be the later ids — front was dropped.
    expect(snap[snap.length - 1].summary).toContain('exit 0')
    // Lifetime event counter keeps counting past the buffer cap.
    expect(getStats().events).toBe(1100)
  })

  it('clear() empties the buffer', () => {
    recordEvent('runspec:hosts_updated', {})
    expect(getSnapshot()).toHaveLength(1)
    clear()
    expect(getSnapshot()).toHaveLength(0)
    expect(getStats().total).toBe(0)
  })
})

describe('getSnapshot reference stability', () => {
  it('returns the same reference until a mutation occurs', () => {
    const a = getSnapshot()
    const b = getSnapshot()
    expect(a).toBe(b) // no mutation → identical reference (no render loop)

    recordEvent('runspec:hosts_updated', {})
    const c = getSnapshot()
    expect(c).not.toBe(a) // mutation → new reference
  })

  it('notifies subscribers on mutation only', () => {
    const listener = vi.fn()
    const unsub = subscribe(listener)
    recordEvent('runspec:hosts_updated', {})
    recordEvent('runspec:runnables_updated', {})
    expect(listener).toHaveBeenCalledTimes(2)
    unsub()
    recordEvent('runspec:hosts_updated', {})
    expect(listener).toHaveBeenCalledTimes(2) // unsubscribed → no further calls
  })
})

describe('bridge call recording', () => {
  it('records start → end with status, duration, and category', () => {
    const id = recordCallStart('get_hosts', ['all'])
    let entry = getSnapshot().find(e => e.id === id)!
    expect(entry.status).toBe('pending')
    expect(entry.direction).toBe('call')
    expect(entry.category).toBe('bridge-call')

    recordCallEnd(id, [{ name: 'local' }], 12.4)
    entry = getSnapshot().find(e => e.id === id)!
    expect(entry.status).toBe('ok')
    expect(entry.durationMs).toBeCloseTo(12.4)
    expect((entry.detail as { result: unknown }).result).toEqual([{ name: 'local' }])
    expect(getStats().calls).toBe(1)
    expect(getStats().errors).toBe(0)
  })

  it('records errors with message and flips category', () => {
    const id = recordCallStart('test_host', ['box'])
    recordCallError(id, new Error('ssh timeout'), 3000)
    const entry = getSnapshot().find(e => e.id === id)!
    expect(entry.status).toBe('error')
    expect(entry.category).toBe('error')
    expect(entry.error).toBe('ssh timeout')
    expect(getStats().errors).toBe(1)
  })
})

describe('event category + summary mapping', () => {
  const cases: Array<[string, unknown, string]> = [
    ['runspec:output', { id: '1', stream: 'stderr', line: 'oops' }, 'output'],
    ['runspec:token', { id: '1', token: 'hi' }, 'token'],
    ['runspec:tool_start', { id: '1', tool_name: 'disk' }, 'tool'],
    ['runspec:tool_end', { id: '1', tool_name: 'disk' }, 'tool'],
    ['runspec:run_end', { id: '1', exit_code: 0, duration_ms: 7 }, 'run_end'],
    ['runspec:chat_usage', { id: '1', input_tokens: 5, output_tokens: 9 }, 'usage'],
    ['runspec:hosts_updated', {}, 'discovery'],
    ['runspec:runnables_updated', {}, 'discovery'],
    ['runspec:ssh', { level: 'INFO', message: 'Connected' }, 'ssh'],
    ['runspec:invoke_runnable', { runnable: 'x' }, 'event'],
  ]

  it.each(cases)('%s → %s category', (type, detail, category) => {
    recordEvent(type, detail)
    const last = getSnapshot()[getSnapshot().length - 1]
    expect(last.category).toBe(category)
  })

  it('summarizes run_end with exit code and duration', () => {
    recordEvent('runspec:run_end', { id: '1', exit_code: 2, duration_ms: 42 })
    const last = getSnapshot()[0]
    expect(last.summary).toBe('exit 2 in 42ms')
  })

  it('summarizes ssh records with level prefix', () => {
    recordEvent('runspec:ssh', { level: 'ERROR', message: 'Error reading SSH protocol banner' })
    const last = getSnapshot()[0]
    expect(last.summary).toBe('[ERROR] Error reading SSH protocol banner')
  })
})

describe('ssh error rows', () => {
  it('marks ERROR-level ssh records as error and counts them', () => {
    recordEvent('runspec:ssh', { level: 'ERROR', message: 'banner timeout' })
    const last = getSnapshot()[0]
    expect(last.status).toBe('error')
    expect(getStats().errors).toBe(1)
  })

  it('leaves INFO-level ssh records as ok and uncounted', () => {
    recordEvent('runspec:ssh', { level: 'INFO', message: 'Authentication (publickey) successful' })
    const last = getSnapshot()[0]
    expect(last.status).toBe('ok')
    expect(getStats().errors).toBe(0)
  })
})

describe('coalescing', () => {
  it('collapses repeated same-id tokens into one entry with a count', () => {
    recordEvent('runspec:token', { id: 'c1', token: 'a' })
    recordEvent('runspec:token', { id: 'c1', token: 'b' })
    recordEvent('runspec:token', { id: 'c1', token: 'c' })
    const snap = getSnapshot()
    expect(snap).toHaveLength(1)
    expect(snap[0].count).toBe(3)
    expect(snap[0].summary).toBe('abc')
    // Lifetime event counter still counts every token.
    expect(getStats().events).toBe(3)
  })

  it('does not coalesce across different ids', () => {
    recordEvent('runspec:token', { id: 'c1', token: 'a' })
    recordEvent('runspec:token', { id: 'c2', token: 'b' })
    expect(getSnapshot()).toHaveLength(2)
  })

  it('an interleaved event breaks the coalesced run', () => {
    recordEvent('runspec:token', { id: 'c1', token: 'a' })
    recordEvent('runspec:run_end', { id: 'c1', exit_code: 0, duration_ms: 1 })
    recordEvent('runspec:token', { id: 'c1', token: 'b' })
    const snap = getSnapshot()
    expect(snap).toHaveLength(3)
    expect(snap[0].count).toBe(1)
    expect(snap[2].count).toBe(1)
  })

  it('separates output streams by stream name', () => {
    recordEvent('runspec:output', { id: 'o1', stream: 'stdout', line: 'x' })
    recordEvent('runspec:output', { id: 'o1', stream: 'stderr', line: 'y' })
    expect(getSnapshot()).toHaveLength(2)
  })
})

describe('pause', () => {
  it('drops new records while paused', () => {
    setPaused(true)
    recordEvent('runspec:hosts_updated', {})
    recordCallStart('get_hosts', [])
    expect(getSnapshot()).toHaveLength(0)
    setPaused(false)
    recordEvent('runspec:hosts_updated', {})
    expect(getSnapshot()).toHaveLength(1)
  })
})

describe('installDispatchTap', () => {
  let fakeWindow: { dispatchEvent: (e: Event) => boolean }
  let original: ReturnType<typeof vi.fn>

  beforeEach(() => {
    original = vi.fn(() => true)
    // vitest 4's Mock type isn't directly assignable to the precise
    // dispatchEvent signature; cast (original stays a Mock for .mock access).
    fakeWindow = { dispatchEvent: original as unknown as (e: Event) => boolean }
    // Minimal globals so the tap installs and CustomEvent works in node env.
    vi.stubGlobal('window', fakeWindow)
    if (typeof CustomEvent === 'undefined') {
      // Node <19 fallback — provide a tiny CustomEvent.
      vi.stubGlobal(
        'CustomEvent',
        class<T> extends Event {
          detail: T
          constructor(type: string, init?: { detail?: T }) {
            super(type)
            this.detail = init?.detail as T
          }
        },
      )
    }
    installDispatchTap()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('records runspec:* events and calls the original dispatch once', () => {
    const evt = new CustomEvent('runspec:hosts_updated', { detail: { n: 1 } })
    const ret = fakeWindow.dispatchEvent(evt)
    expect(ret).toBe(true)
    expect(original).toHaveBeenCalledTimes(1)
    expect(original).toHaveBeenCalledWith(evt)
    expect(getSnapshot()).toHaveLength(1)
    expect(getSnapshot()[0].name).toBe('runspec:hosts_updated')
  })

  it('ignores non-runspec events but still dispatches them', () => {
    const evt = new CustomEvent('click', { detail: {} })
    fakeWindow.dispatchEvent(evt)
    expect(original).toHaveBeenCalledTimes(1)
    expect(getSnapshot()).toHaveLength(0)
  })

  it('is idempotent — a second install does not double-wrap', () => {
    const wrappedOnce = fakeWindow.dispatchEvent
    installDispatchTap()
    expect(fakeWindow.dispatchEvent).toBe(wrappedOnce)
    fakeWindow.dispatchEvent(new CustomEvent('runspec:hosts_updated', { detail: {} }))
    expect(original).toHaveBeenCalledTimes(1)
    expect(getSnapshot()).toHaveLength(1)
  })
})

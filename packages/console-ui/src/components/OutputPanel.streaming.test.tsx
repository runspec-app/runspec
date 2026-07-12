/**
 * Component tests for the large-output streaming path in OutputPanel.
 *
 * A runnable that dumps thousands of lines (e.g. 10k lines of pretty-printed
 * JSON) used to fire one setBlocks per line — O(n²) churn plus a React render
 * per line — hanging the Console card for minutes. These cover the fixes:
 *   1. runspec:output events are coalesced and flushed once per animation frame
 *      (batching), so many lines become one state update.
 *   2. While streaming, the raw view renders only a bounded live tail.
 *   3. On run_end the buffer is flushed before the block is marked done, so the
 *      structured parse runs on the complete line set.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { useEffect } from 'react'

import { OutputPanel, useInvocationBlocks } from './OutputPanel'
import type { InvocationBlock } from './OutputPanel'

// The real `bridge` is a Proxy that waits on window.pywebview (2.5s fallback) —
// not spy-able and slow. Replace it with a no-op stub, as the confirm test does.
vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn() }),
}))

// A hand-driven requestAnimationFrame queue so a test can dispatch several
// output events and then flush them in one frame — the batching under test.
let rafCbs: FrameRequestCallback[] = []
beforeEach(() => {
  rafCbs = []
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
    rafCbs.push(cb)
    return rafCbs.length
  }) as typeof requestAnimationFrame
  globalThis.cancelAnimationFrame = (() => {}) as typeof cancelAnimationFrame
})
afterEach(() => vi.restoreAllMocks())

const flushRaf = () => {
  const cbs = rafCbs
  rafCbs = []
  cbs.forEach(cb => cb(0))
}

// Harness: wires useInvocationBlocks to OutputPanel and hands the test `addBlock`.
function Harness({ onReady }: { onReady: (add: (b: InvocationBlock) => void) => void }) {
  const { blocks, addBlock } = useInvocationBlocks()
  useEffect(() => { onReady(addBlock) }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  return <OutputPanel blocks={blocks} />
}

function runBlock(id: string): InvocationBlock {
  return { id, type: 'run', label: `/dump`, startedAt: new Date().toISOString(), lines: [], segments: [], currentText: '', done: false }
}

function emitLine(id: string, line: string, stream: 'stdout' | 'stderr' = 'stdout') {
  window.dispatchEvent(new CustomEvent('runspec:output', { detail: { id, line, stream } }))
}
function emitEnd(id: string, exit_code = 0) {
  window.dispatchEvent(new CustomEvent('runspec:run_end', { detail: { id, exit_code, duration_ms: 5 } }))
}

function mount(): (b: InvocationBlock) => void {
  let add!: (b: InvocationBlock) => void
  render(<Harness onReady={a => (add = a)} />)
  return add
}

describe('OutputPanel streaming', () => {
  it('coalesces many output events into a single flush per frame', () => {
    const add = mount()
    act(() => add(runBlock('r1')))

    // Dispatch three lines without flushing a frame — they stay buffered.
    act(() => { emitLine('r1', 'alpha'); emitLine('r1', 'beta'); emitLine('r1', 'gamma') })
    expect(screen.queryByText(/alpha/)).toBeNull()

    // One frame delivers all three at once.
    act(() => flushRaf())
    expect(screen.getByText(/alpha/)).toBeTruthy()
    expect(screen.getByText(/beta/)).toBeTruthy()
    expect(screen.getByText(/gamma/)).toBeTruthy()
  })

  it('renders only a bounded tail while streaming a huge dump', () => {
    const add = mount()
    act(() => add(runBlock('r2')))

    act(() => {
      for (let i = 0; i < 1000; i++) emitLine('r2', `line-${i}`)
      flushRaf()
    })

    // The tail note is shown, the oldest line is dropped, the newest is kept.
    expect(screen.getByText(/showing the last 500 of 1,000 lines/)).toBeTruthy()
    expect(screen.queryByText(/\bline-0\b/)).toBeNull()
    expect(screen.getByText(/line-999/)).toBeTruthy()
  })

  it('flushes buffered lines before completing so the JSON parse sees them all', () => {
    const add = mount()
    act(() => add(runBlock('r3')))

    // Emit a JSON array-of-objects split across lines, then end in the same tick.
    act(() => {
      emitLine('r3', '[{"host": "box",')
      emitLine('r3', '"n": 1}]')
      emitEnd('r3')
    })

    // Completed run parsed the full (batched) output into the structured table.
    expect(screen.getByText('box')).toBeTruthy()
  })
})

/**
 * Component tests for the deferred render of a large completed run.
 *
 * When a run completes, the parse + table build runs synchronously in the same
 * commit that marks the block done — a multi-second UI freeze for a large result,
 * with no signal (the in-flight strip tracks the backend process, which is
 * already gone). A heavy result now paints a "Rendering result…" placeholder
 * first and commits the heavy build two frames later; a small result renders
 * inline with no placeholder. These drive OutputPanel with completed run blocks
 * and a hand-driven requestAnimationFrame queue.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'

import { OutputPanel } from './OutputPanel'
import type { InvocationBlock } from './OutputPanel'

vi.mock('../bridge', () => ({ bridge: new Proxy({}, { get: () => vi.fn() }) }))

// Hand-driven rAF so a test can assert the placeholder paints before the
// deferred (two-frame) heavy render commits.
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

function doneRunBlock(id: string, stdout: string): InvocationBlock {
  return {
    id, type: 'run', label: '/dump', startedAt: new Date().toISOString(),
    lines: [{ id, line: stdout, stream: 'stdout' }],
    segments: [], currentText: '', done: true, exitCode: 0, durationMs: 5,
  }
}

// A JSON array-of-objects big enough (>100 KB) to trip HEAVY_RENDER_CHARS.
const bigJson = JSON.stringify(
  Array.from({ length: 5000 }, (_, i) => ({ idx: i, host: `box-${i}`, note: 'x'.repeat(20) })),
)

const dataRows = () => screen.getAllByRole('row').filter(r => r.querySelector('td'))

describe('OutputPanel deferred render', () => {
  it('shows a rendering placeholder for a heavy result, then the table', () => {
    render(<OutputPanel blocks={[doneRunBlock('r1', bigJson)]} />)

    // First paint: placeholder, no table yet (the parse hasn't run).
    expect(screen.getByText(/Rendering result/)).toBeTruthy()
    expect(screen.queryByText('box-0')).toBeNull()

    // Two frames later the heavy build commits: placeholder gone, table present.
    act(() => flushRaf())
    act(() => flushRaf())
    expect(screen.queryByText(/Rendering result/)).toBeNull()
    expect(screen.getByText('box-0')).toBeTruthy()
    expect(dataRows().length).toBeGreaterThan(0)
  })

  it('renders a small result inline with no placeholder', () => {
    const small = JSON.stringify([{ host: 'box', n: 1 }])
    render(<OutputPanel blocks={[doneRunBlock('r2', small)]} />)

    // No deferral: table is present immediately, placeholder never shown.
    expect(screen.queryByText(/Rendering result/)).toBeNull()
    expect(screen.getByText('box')).toBeTruthy()
  })
})

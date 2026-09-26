/**
 * Component tests for the sticky bottom activity indicator in OutputPanel.
 *
 * A long agent turn stacks many tool cards, scrolling the card's own spinner far
 * above the fold — so an in-flight turn looked stalled. The footer pins a live
 * "still working" signal (the current step + elapsed time) at the bottom of the
 * panel, and disappears the moment the turn is done. These cover:
 *   1. a running tool surfaces its short name in the footer;
 *   2. a streaming reply (no running tool) shows "Responding…";
 *   3. a finished turn shows no activity footer.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

import { OutputPanel } from './OutputPanel'
import type { InvocationBlock, BlockSegment } from './OutputPanel'

// The real `bridge` waits on window.pywebview — stub it, as the other tests do.
vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn() }),
}))

function chatBlock(over: Partial<InvocationBlock> = {}): InvocationBlock {
  return {
    id: 'c1', sessionId: 's1', type: 'chat', label: 'do the thing',
    startedAt: new Date().toISOString(), lines: [], segments: [],
    currentText: '', done: false, ...over,
  }
}

const runningTool: BlockSegment = {
  kind: 'tool',
  entry: { name: 'linux-box__restart-service', input: {}, running: true },
}

describe('OutputPanel activity indicator', () => {
  it('shows the running tool name while a tool is executing', () => {
    render(<OutputPanel blocks={[chatBlock({ segments: [runningTool] })]} />)
    expect(screen.getByText('Running')).toBeTruthy()
    // The host prefix is stripped to the bare tool name (shown in the footer;
    // the tool card renders it too, hence getAllByText).
    expect(screen.getAllByText('restart-service').length).toBeGreaterThan(0)
  })

  it('shows "Responding…" while the model streams text with no tool running', () => {
    render(<OutputPanel blocks={[chatBlock({ currentText: 'here is my answer' })]} />)
    expect(screen.getByText('Responding…')).toBeTruthy()
  })

  it('shows no activity footer once the turn is done', () => {
    render(<OutputPanel blocks={[chatBlock({ done: true, segments: [{ kind: 'text', text: 'done' }] })]} />)
    expect(screen.queryByText('Running')).toBeNull()
    expect(screen.queryByText('Responding…')).toBeNull()
    expect(screen.queryByText('Working…')).toBeNull()
  })
})

describe('OutputPanel activity indicator — thinking summaries', () => {
  it('shows the latest reasoning line from runspec:thinking while between steps', async () => {
    const { act } = await import('@testing-library/react')
    render(<OutputPanel blocks={[chatBlock()]} />)
    act(() => {
      window.dispatchEvent(new CustomEvent('runspec:thinking', { detail: { id: 'c1', text: 'Listing kernels.\nChecking /boot usage' } }))
    })
    expect(screen.getByText('Thinking…')).toBeTruthy()
    expect(screen.getByText('Checking /boot usage')).toBeTruthy()
  })

  it('ignores thinking for another turn and yields to a running tool', async () => {
    const { act } = await import('@testing-library/react')
    render(<OutputPanel blocks={[chatBlock({ segments: [runningTool] })]} />)
    act(() => {
      window.dispatchEvent(new CustomEvent('runspec:thinking', { detail: { id: 'c1', text: 'planning' } }))
      window.dispatchEvent(new CustomEvent('runspec:thinking', { detail: { id: 'other', text: 'elsewhere' } }))
    })
    expect(screen.queryByText('Thinking…')).toBeNull()
    expect(screen.getByText('Running')).toBeTruthy()
    expect(screen.queryByText('elsewhere')).toBeNull()
  })
})

describe('lastLine', () => {
  it('returns the last non-empty trimmed line', async () => {
    const { lastLine } = await import('./OutputPanel')
    expect(lastLine('a\n  b  \n\n')).toBe('b')
    expect(lastLine('')).toBe('')
  })
})

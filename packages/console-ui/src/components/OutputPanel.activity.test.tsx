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
import { render, screen, fireEvent } from '@testing-library/react'

import { OutputPanel } from './OutputPanel'
import type { InvocationBlock, BlockSegment } from './OutputPanel'

/** The pill shows only when you've scrolled away from the latest output. */
function scrollAway() {
  const el = document.querySelector('[data-testid="output-scroll"]') as HTMLElement
  Object.defineProperty(el, 'scrollHeight', { value: 2000, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: 200, configurable: true })
  el.scrollTop = 0
  fireEvent.scroll(el)
}

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
    scrollAway()
    expect(screen.getByText('Running')).toBeTruthy()
    // The host prefix is stripped to the bare tool name (shown in the footer;
    // the tool card renders it too, hence getAllByText).
    expect(screen.getAllByText('restart-service').length).toBeGreaterThan(0)
  })

  it('shows "Responding…" while the model streams text with no tool running', () => {
    render(<OutputPanel blocks={[chatBlock({ currentText: 'here is my answer' })]} />)
    scrollAway()
    expect(screen.getByText('Responding…')).toBeTruthy()
  })

  it('shows no activity footer once the turn is done', () => {
    render(<OutputPanel blocks={[chatBlock({ done: true, segments: [{ kind: 'text', text: 'done' }] })]} />)
    scrollAway()
    expect(screen.queryByText('Running')).toBeNull()
    expect(screen.queryByText('Responding…')).toBeNull()
    expect(screen.queryByText('Working…')).toBeNull()
  })
})

describe('OutputPanel activity indicator — thinking summaries', () => {
  it('shows the latest reasoning line from runspec:thinking while between steps', async () => {
    const { act } = await import('@testing-library/react')
    render(<OutputPanel blocks={[chatBlock()]} />)
    scrollAway()
    act(() => {
      window.dispatchEvent(new CustomEvent('runspec:thinking', { detail: { id: 'c1', text: 'Listing kernels.\nChecking /boot usage' } }))
    })
    expect(screen.getByText('Thinking…')).toBeTruthy()
    expect(screen.getByText('Checking /boot usage')).toBeTruthy()
  })

  it('ignores thinking for another turn and yields to a running tool', async () => {
    const { act } = await import('@testing-library/react')
    render(<OutputPanel blocks={[chatBlock({ segments: [runningTool] })]} />)
    scrollAway()
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

describe('thinkingTail', () => {
  it('keeps a short line whole', async () => {
    const { thinkingTail } = await import('./OutputPanel')
    expect(thinkingTail('plan\nChecking /boot usage')).toBe('Checking /boot usage')
  })

  it('keeps the newest words of a long line behind a leading ellipsis', async () => {
    const { thinkingTail } = await import('./OutputPanel')
    const words = Array.from({ length: 80 }, (_, i) => `word${i}`).join(' ')
    const out = thinkingTail(words, 100)
    expect(out.startsWith('…')).toBe(true)
    expect(out.endsWith('word79')).toBe(true)
    expect(out.length).toBeLessThanOrEqual(101)
    expect(out.slice(1).startsWith('word')).toBe(true) // cut at a word boundary
  })
})

describe('OutputPanel activity indicator — longer summaries', () => {
  it('renders a multi-row (wrapping, clamped) summary rather than a one-line ellipsis', async () => {
    const { act } = await import('@testing-library/react')
    render(<OutputPanel blocks={[chatBlock()]} />)
    scrollAway()
    const long = 'The user wants the disk usage on web-01 so I will check /var first and then /home before deciding'
    act(() => {
      window.dispatchEvent(new CustomEvent('runspec:thinking', { detail: { id: 'c1', text: long } }))
    })
    const el = screen.getByTestId('thinking-summary')
    expect(el.textContent).toBe(long)
    expect(el.style.whiteSpace).toBe('normal')
    expect(el.style.maxWidth).toBe('640px')
  })
})

describe('OutputPanel activity pill placement', () => {
  it('stays hidden while you are at the latest output', () => {
    render(<OutputPanel blocks={[chatBlock({ segments: [runningTool] })]} />)
    expect(screen.queryByTestId('activity-label')).toBeNull()
    scrollAway()
    expect(screen.getByTestId('activity-label').textContent).toBe('Running')
  })
})

describe('a denied call', () => {
  it('shows a stop mark and no duration, not a green check', () => {
    const denied: BlockSegment = {
      kind: 'tool',
      entry: { name: 'flush-dns', input: {}, running: false, operatorDecision: 'denied', durationMs: 7100 },
    }
    render(<OutputPanel blocks={[chatBlock({ done: true, segments: [denied] })]} />)
    expect(screen.getByLabelText('denied, did not run')).toBeTruthy()
    expect(screen.queryByLabelText('done')).toBeNull()
    expect(screen.queryByText('7.1s')).toBeNull()
  })
})

describe('model usage on a chat card', () => {
  it('sits behind a Usage button, not in the header', async () => {
    const { fireEvent: fe } = await import('@testing-library/react')
    render(<OutputPanel blocks={[chatBlock({ done: true, durationMs: 1000, tokenUsage: { input: 100, output: 20, cacheRead: 900, cacheCreation: 0 } as InvocationBlock['tokenUsage'] })]} />)
    expect(screen.queryByText(/% cached/)).toBeNull()
    fe.click(screen.getByRole('button', { name: 'Model usage for this turn' }))
    expect((await screen.findByTestId('card-usage')).textContent).toContain('90% of the prompt served from cache')
  })
})

describe('a finished call says where it ran and who approved it', () => {
  it('shows the host and "approved by you"', () => {
    const approved: BlockSegment = {
      kind: 'tool',
      entry: { name: 'run_runnable', input: { id: 'flush-dns', host: 'web-01' }, running: false, operatorDecision: 'approved', durationMs: 900 },
    }
    render(<OutputPanel blocks={[chatBlock({ done: true, segments: [approved] })]} />)
    expect(screen.getByTestId('tool-decision').textContent).toBe('approved by you')
    expect(screen.getByTestId('tool-host').textContent).toBe('on web-01')
  })
})

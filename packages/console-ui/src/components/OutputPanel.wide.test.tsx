/**
 * Cards fill the window (an ultrawide one included), so wide tables and logs
 * use the room instead of scrolling sideways. Prose keeps a readable measure,
 * a collapsed tool row keeps the old column width (its status stays beside its
 * name), and a narrow JSON table isn't stretched across the screen.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { useEffect } from 'react'

import { OutputPanel, useInvocationBlocks, StructuredOutput } from './OutputPanel'
import type { InvocationBlock } from './OutputPanel'
import { Markdown, PROSE_MEASURE } from './Markdown'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn() }),
}))

beforeEach(() => {
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 1 }) as typeof requestAnimationFrame
  globalThis.cancelAnimationFrame = (() => {}) as typeof cancelAnimationFrame
})

function Harness({ onReady }: { onReady: (add: (b: InvocationBlock) => void) => void }) {
  const { blocks, addBlock } = useInvocationBlocks()
  useEffect(() => { onReady(addBlock) }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  return <OutputPanel blocks={blocks} />
}

const emit = (name: string, detail: Record<string, unknown>) =>
  window.dispatchEvent(new CustomEvent(name, { detail }))

describe('cards fill a wide window', () => {
  it('leaves the card column uncapped', () => {
    render(<OutputPanel blocks={[{
      id: 'r1', sessionId: 's1', type: 'run', label: 'get-alerts', startedAt: new Date().toISOString(),
      lines: [], segments: [], currentText: '', done: true,
    }]} />)
    const column = screen.getByTestId('output-scroll').firstElementChild as HTMLElement
    expect(column.style.maxWidth).toBe('')
    expect(column.style.width).toBe('100%')
  })

  it('keeps a collapsed tool row at the old width and opens it to the full card', () => {
    let add!: (b: InvocationBlock) => void
    render(<Harness onReady={a => (add = a)} />)
    act(() => add({
      id: 'c1', sessionId: 's1', type: 'chat', label: 'inventory', startedAt: new Date().toISOString(),
      lines: [], segments: [], currentText: '', done: false,
    }))
    act(() => emit('runspec:tool_start', { id: 'c1', call_id: 't1', tool_name: 'web-01__inventory', tool_input: {} }))
    act(() => emit('runspec:tool_end', { id: 'c1', call_id: 't1', tool_name: 'web-01__inventory', output: '[{"a":1}]' }))
    const box = () => document.querySelector('.rs-tool-row')!.parentElement as HTMLElement
    expect(box().style.maxWidth).toBe('952px')
    fireEvent.click(document.querySelector('.rs-tool-row')!)
    expect(box().style.maxWidth).toBe('')
  })

  it('caps prose at a readable measure but not tables or code', () => {
    const { container } = render(
      <Markdown>{'A paragraph.\n\n- a list\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\ncode\n```'}</Markdown>,
    )
    expect(container.querySelector('p')!.style.maxWidth).toBe(PROSE_MEASURE)
    expect(container.querySelector('ul')!.style.maxWidth).toBe(PROSE_MEASURE)
    expect(container.querySelector('table')!.parentElement!.style.maxWidth).toBe('')
    expect(container.querySelector('pre')!.style.maxWidth).toBe('')
  })

  it("doesn't stretch a JSON table across the whole card", () => {
    const { container } = render(<StructuredOutput text={JSON.stringify([{ host: 'web-01', ok: true }])} />)
    const table = container.querySelector('table') as HTMLElement
    expect(table.style.width).toBe('')
    expect(table.style.minWidth).toBe('min(100%, 900px)')
  })
})

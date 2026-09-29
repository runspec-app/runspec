/**
 * Reasoning summaries in the turn card (option C).
 *
 * While the model thinks, the summary streams into an expanded "Thinking…" box
 * in the card. When the step it led to starts, the box collapses into a bulb on
 * that tool row ("why it ran this", shown when the row is opened); the thought
 * behind the final reply collapses into a "Reasoning behind this reply" line.
 * A reopened conversation rebuilds both from the saved transcript.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { useEffect } from 'react'

import { OutputPanel, useInvocationBlocks, fmtThought } from './OutputPanel'
import type { InvocationBlock } from './OutputPanel'
import { conversationToBlocks } from '../views/ConsoleView'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn() }),
}))

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

function Harness({ onReady }: { onReady: (add: (b: InvocationBlock) => void) => void }) {
  const { blocks, addBlock } = useInvocationBlocks()
  useEffect(() => { onReady(addBlock) }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  return <OutputPanel blocks={blocks} />
}

function mount(): (b: InvocationBlock) => void {
  let add!: (b: InvocationBlock) => void
  render(<Harness onReady={a => (add = a)} />)
  return add
}

const chat = (id: string): InvocationBlock => ({
  id, sessionId: 's1', type: 'chat', label: 'why is disk full?', startedAt: new Date().toISOString(),
  lines: [], segments: [], currentText: '', done: false,
})
const emit = (name: string, detail: Record<string, unknown>) =>
  window.dispatchEvent(new CustomEvent(name, { detail }))

describe('reasoning in the turn card', () => {
  it('shows the thought expanded while the model thinks', () => {
    const add = mount()
    act(() => add(chat('c1')))
    act(() => { emit('runspec:thinking', { id: 'c1', text: 'Check which directory ' }); emit('runspec:thinking', { id: 'c1', text: 'is biggest.' }) })
    act(() => flushRaf())
    expect(screen.getByTestId('live-thought').textContent).toContain('Check which directory is biggest.')
  })

  it('collapses the thought into the tool row it led to', () => {
    const add = mount()
    act(() => add(chat('c1')))
    act(() => { emit('runspec:thinking', { id: 'c1', text: 'Check which directory is biggest.' }); flushRaf() })
    act(() => emit('runspec:tool_start', {
      id: 'c1', call_id: 't1', tool_name: 'web-01__du', tool_input: { path: '/' },
      thinking: 'Check which directory is biggest.', thinking_ms: 4200,
    }))
    expect(screen.queryByTestId('live-thought')).toBeNull()
    expect(screen.getByLabelText('Has reasoning')).toBeTruthy()
    // Opening the row shows why it ran.
    act(() => emit('runspec:tool_end', { id: 'c1', call_id: 't1', tool_name: 'web-01__du', output: '31G /var' }))
    fireEvent.click(screen.getByText('du'))
    expect(screen.getByTestId('tool-thought').textContent).toBe('Check which directory is biggest.')
    expect(screen.getByText(/why it ran this · thought for 4s/i)).toBeTruthy()
  })

  it('hides the live box once the reply starts streaming', () => {
    const add = mount()
    act(() => add(chat('c1')))
    act(() => { emit('runspec:thinking', { id: 'c1', text: 'Logs dominate.' }); flushRaf() })
    act(() => { emit('runspec:token', { id: 'c1', token: 'It is the logs.' }); flushRaf() })
    expect(screen.queryByTestId('live-thought')).toBeNull()
  })

  it('collapses the reply thought into a "Reasoning behind this reply" line', () => {
    const add = mount()
    act(() => add(chat('c1')))
    act(() => { emit('runspec:thinking', { id: 'c1', text: 'Logs dominate.' }); emit('runspec:token', { id: 'c1', token: 'It is the logs.' }); flushRaf() })
    act(() => emit('runspec:reply_thinking', { id: 'c1', text: 'Logs dominate.', ms: 2500 }))
    act(() => emit('runspec:run_end', { id: 'c1', exit_code: 0, duration_ms: 9 }))
    const line = screen.getByRole('button', { name: /reasoning behind this reply · 3s/i })
    expect(screen.queryByTestId('reply-thought')).toBeNull()
    fireEvent.click(line)
    expect(screen.getByTestId('reply-thought').textContent).toBe('Logs dominate.')
  })

  it('drops an unattached live thought when the turn ends', () => {
    const add = mount()
    act(() => add(chat('c1')))
    act(() => { emit('runspec:thinking', { id: 'c1', text: 'half a thought' }); flushRaf() })
    act(() => emit('runspec:run_end', { id: 'c1', exit_code: 0, duration_ms: 9, stopped: true }))
    expect(screen.queryByTestId('live-thought')).toBeNull()
  })
})

describe('reopened conversations keep their reasoning', () => {
  it('rebuilds tool thoughts and the reply thought from the saved turn', () => {
    const [block] = conversationToBlocks({
      id: 'conv1', title: 't',
      turns: [{
        user: 'why?', assistant: 'logs',
        tools: [{ name: 'web-01__du', input: {}, output: 'x', thinking: 'check dirs', thinking_ms: 1200 }],
        reply_thinking: { text: 'logs win', ms: 800 },
      }],
    })
    const seg = block.segments[0]
    expect(seg.kind === 'tool' && seg.entry.thinking).toBe('check dirs')
    expect(seg.kind === 'tool' && seg.entry.thinkingMs).toBe(1200)
    expect(block.replyThinking).toEqual({ text: 'logs win', ms: 800 })
  })
})

describe('fmtThought', () => {
  it('formats thought durations', () => {
    expect(fmtThought(0)).toBe('')
    expect(fmtThought(undefined)).toBe('')
    expect(fmtThought(400)).toBe('<1s')
    expect(fmtThought(4200)).toBe('4s')
  })
})

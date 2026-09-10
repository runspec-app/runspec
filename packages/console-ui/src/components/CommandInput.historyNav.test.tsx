/**
 * Component test for the composer's ArrowUp/ArrowDown history navigation.
 *
 * Regression guard: a bare ArrowUp on a freshly typed prompt used to
 * unconditionally preventDefault and overwrite the text. With an empty history
 * it wrote `history[-1] ?? '' = ''` — blanking the prompt — and left
 * historyIndex at -1, so the follow-up ArrowDown (guarded by historyIndex < 0)
 * couldn't restore it. The typed prompt was gone forever. ArrowDown had the
 * guard; ArrowUp didn't. These tests drive the real textarea + caret and assert
 * on the DOM so the asymmetry can't come back.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import { CommandInput } from './CommandInput'

function noop() {}

function renderComposer(history: string[]) {
  render(
    <CommandInput
      runnables={[]}
      allRunnables={[]}
      selectedHost=""
      runbooks={[]}
      savedRuns={[]}
      onRunRunnable={noop}
      onOpenForm={noop}
      onOpenRunbook={noop}
      onOpenSavedRun={noop}
      onSendChat={noop}
      history={history}
    />,
  )
  return screen.getByPlaceholderText(/ask the assistant/i) as HTMLTextAreaElement
}

// Set the value the way the user would, then place the caret.
function typePrompt(el: HTMLTextAreaElement, text: string, caret: 'start' | 'end') {
  fireEvent.change(el, { target: { value: text } })
  const pos = caret === 'start' ? 0 : text.length
  el.setSelectionRange(pos, pos)
}

describe('CommandInput history navigation', () => {
  it('does NOT blank a typed prompt on ArrowUp when history is empty (the regression)', () => {
    const el = renderComposer([])
    typePrompt(el, 'a long prompt I do not want to lose', 'start')
    fireEvent.keyDown(el, { key: 'ArrowUp' })
    expect(el.value).toBe('a long prompt I do not want to lose')
  })

  it('leaves a multi-line prompt alone on ArrowUp when the caret is not at the start', () => {
    const el = renderComposer(['old command'])
    typePrompt(el, 'line one\nline two', 'end')
    fireEvent.keyDown(el, { key: 'ArrowUp' })
    // Caret was mid-text, so ArrowUp is a caret move — the draft is untouched.
    expect(el.value).toBe('line one\nline two')
  })

  it('recalls history on ArrowUp only when the caret is at the start, and ArrowDown restores the stashed draft', () => {
    const el = renderComposer(['first', 'second'])
    typePrompt(el, 'draft', 'start')

    fireEvent.keyDown(el, { key: 'ArrowUp' })
    expect(el.value).toBe('second') // most recent first

    fireEvent.keyDown(el, { key: 'ArrowUp' }) // already browsing — pages freely
    expect(el.value).toBe('first')

    fireEvent.keyDown(el, { key: 'ArrowUp' }) // clamps at the oldest
    expect(el.value).toBe('first')

    fireEvent.keyDown(el, { key: 'ArrowDown' })
    expect(el.value).toBe('second')

    fireEvent.keyDown(el, { key: 'ArrowDown' }) // back to the bottom → stashed draft
    expect(el.value).toBe('draft')
  })

  it('does not clear text on a bare ArrowDown when not browsing history', () => {
    const el = renderComposer(['first'])
    typePrompt(el, 'still typing', 'end')
    fireEvent.keyDown(el, { key: 'ArrowDown' })
    expect(el.value).toBe('still typing')
  })
})

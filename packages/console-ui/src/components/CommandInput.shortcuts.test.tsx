/**
 * Command-bar shortcuts: Esc stops the running turn; the focus event from
 * Ctrl+L / `/` focuses the box, and `/` opens the runnable menu only into an
 * empty box (a draft is never overwritten).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { CommandInput } from './CommandInput'

function noop() {}

beforeEach(() => { try { localStorage.clear() } catch { /* ignore */ } })

function renderComposer(activeChatId: string | null = null) {
  const onStopChat = vi.fn()
  render(
    <CommandInput
      runnables={[]} allRunnables={[]} selectedHost="" runbooks={[]} savedRuns={[]}
      onRunRunnable={noop} onOpenForm={noop} onOpenRunbook={noop} onOpenSavedRun={noop}
      onSendChat={noop} activeChatId={activeChatId} onStopChat={onStopChat} history={[]}
    />,
  )
  return { onStopChat, el: screen.getByPlaceholderText(/ask the assistant/i) as HTMLTextAreaElement }
}

const focusEvent = (slash: boolean) =>
  act(async () => {
    window.dispatchEvent(new CustomEvent('runspec:focus_composer', { detail: { slash } }))
    await new Promise(r => requestAnimationFrame(() => r(null)))
  })

describe('CommandInput shortcuts', () => {
  it('Esc stops the running turn', () => {
    const { onStopChat, el } = renderComposer('chat-1')
    fireEvent.keyDown(el, { key: 'Escape' })
    expect(onStopChat).toHaveBeenCalledWith('chat-1')
  })

  it('Esc does nothing when no turn is running', () => {
    const { onStopChat, el } = renderComposer(null)
    fireEvent.keyDown(el, { key: 'Escape' })
    expect(onStopChat).not.toHaveBeenCalled()
  })

  it('focuses the box and opens the runnable menu into an empty box', async () => {
    const { el } = renderComposer()
    await focusEvent(true)
    expect(el.value).toBe('/')
    expect(document.activeElement).toBe(el)
  })

  it('keeps a draft instead of replacing it with /', async () => {
    const { el } = renderComposer()
    fireEvent.change(el, { target: { value: 'half-typed' } })
    await focusEvent(true)
    expect(el.value).toBe('half-typed')
    expect(document.activeElement).toBe(el)
  })
})

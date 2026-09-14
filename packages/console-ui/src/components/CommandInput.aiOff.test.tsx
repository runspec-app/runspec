/**
 * The command bar under the `[console] ai` off-switch: plain text is not sent
 * to the assistant (a hint is shown instead and the draft is kept), the
 * placeholder drops the chat/runbook affordances, and `/` still runs runnables.
 * With AI on (the default context) plain text sends as before.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import { CommandInput } from './CommandInput'
import { FeaturesContext } from '../features'

function noop() {}

function renderComposer(ai: boolean, onSendChat = vi.fn()) {
  render(
    <FeaturesContext.Provider value={{ ai }}>
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
        onSendChat={onSendChat}
        history={[]}
      />
    </FeaturesContext.Provider>,
  )
  return onSendChat
}

describe('CommandInput with AI off', () => {
  it('drops the chat and runbook affordances from the placeholder', () => {
    renderComposer(false)
    expect(screen.queryByPlaceholderText(/ask the assistant/i)).toBeNull()
    expect(screen.queryByPlaceholderText(/\$ runbooks/)).toBeNull()
    expect(screen.getByPlaceholderText(/\/ runnables/)).toBeTruthy()
  })

  it('does not send plain text as a chat turn and keeps the draft', () => {
    const onSendChat = renderComposer(false)
    const el = screen.getByPlaceholderText(/\/ runnables/) as HTMLTextAreaElement
    fireEvent.change(el, { target: { value: 'restart nginx please' } })
    fireEvent.keyDown(el, { key: 'Enter' })
    expect(onSendChat).not.toHaveBeenCalled()
    expect(el.value).toBe('restart nginx please')
  })
})

describe('CommandInput with AI on', () => {
  it('sends plain text as a chat turn', () => {
    const onSendChat = renderComposer(true)
    const el = screen.getByPlaceholderText(/ask the assistant/i) as HTMLTextAreaElement
    fireEvent.change(el, { target: { value: 'restart nginx please' } })
    fireEvent.keyDown(el, { key: 'Enter' })
    expect(onSendChat).toHaveBeenCalledWith('restart nginx please')
  })
})

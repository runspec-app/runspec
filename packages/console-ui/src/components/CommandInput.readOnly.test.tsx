/**
 * The command bar with a read-only automation session selected: plain text
 * shows the "switch to a chat" hint instead of sending, and the draft is kept.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// antd's static `message` API renders through its own portal, which jsdom never
// paints — and spying on it at module load hangs vitest, so mock the one call
// under test at the module boundary and assert the call instead.
const { info } = vi.hoisted(() => ({ info: vi.fn() }))
vi.mock('antd', async (importOriginal) => {
  const actual = await importOriginal<typeof import('antd')>()
  return { ...actual, message: { ...actual.message, info } }
})

import { CommandInput, READ_ONLY_SESSION_HINT } from './CommandInput'

beforeEach(() => info.mockClear())

function noop() {}

function renderComposer(readOnlySession: boolean) {
  const onSendChat = vi.fn()
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
      onSendChat={onSendChat}
      readOnlySession={readOnlySession}
      history={[]}
    />,
  )
  return onSendChat
}

describe('CommandInput with an automation session selected', () => {
  it('does not send plain text, keeps the draft and shows the hint', () => {
    const onSendChat = renderComposer(true)
    const el = screen.getByPlaceholderText(/ask the assistant/i) as HTMLTextAreaElement
    fireEvent.change(el, { target: { value: 'what happened?' } })
    fireEvent.keyDown(el, { key: 'Enter' })
    expect(onSendChat).not.toHaveBeenCalled()
    expect(el.value).toBe('what happened?')
    expect(info).toHaveBeenCalledWith(READ_ONLY_SESSION_HINT, 2)
  })

  it('sends as usual when the selected session is a chat', () => {
    const onSendChat = renderComposer(false)
    const el = screen.getByPlaceholderText(/ask the assistant/i) as HTMLTextAreaElement
    fireEvent.change(el, { target: { value: 'what happened?' } })
    fireEvent.keyDown(el, { key: 'Enter' })
    expect(onSendChat).toHaveBeenCalledWith('what happened?')
    expect(info).not.toHaveBeenCalled()
  })
})

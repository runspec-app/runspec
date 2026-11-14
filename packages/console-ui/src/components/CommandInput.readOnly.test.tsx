/**
 * The command bar with a read-only automation session selected: plain text
 * isn't sent: the composer says the session is read-only, keeps the draft,
 * and Enter (or Ask in a new chat) opens a chat to send it in.
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

import { CommandInput } from './CommandInput'

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
  it('says it is read-only, keeps the draft, and Enter opens a new chat to send it in', () => {
    const onNew = vi.fn()
    window.addEventListener('runspec:new_chat', onNew)
    const onSendChat = renderComposer(true)
    const el = screen.getByPlaceholderText(/automation session \(read-only\)/i) as HTMLTextAreaElement
    fireEvent.change(el, { target: { value: 'what happened?' } })
    fireEvent.keyDown(el, { key: 'Enter' })
    expect(onSendChat).not.toHaveBeenCalled()
    expect(el.value).toBe('what happened?')
    expect(onNew).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('ask-in-new-chat'))
    expect(onNew).toHaveBeenCalledTimes(2)
    window.removeEventListener('runspec:new_chat', onNew)
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

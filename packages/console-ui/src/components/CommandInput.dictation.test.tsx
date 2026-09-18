/**
 * The composer's mic button (in-app dictation over the `[voice]` extra).
 *
 * The backend owns the microphone; the UI only renders the button, shows the
 * in-progress line, and appends finalised lines into the textarea. These tests
 * drive the real component against a mocked bridge and the `runspec:dictation_*`
 * window events the Python side dispatches.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'

import { CommandInput } from './CommandInput'

const { dictationStatus, startDictation, stopDictation } = vi.hoisted(() => ({
  dictationStatus: vi.fn(),
  startDictation: vi.fn(),
  stopDictation: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'dictation_status') return dictationStatus
      if (p === 'start_dictation') return startDictation
      if (p === 'stop_dictation') return stopDictation
      return vi.fn()
    },
  }),
}))

function noop() {}

function renderComposer(onSendChat = vi.fn()) {
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
      history={[]}
    />,
  )
  return {
    onSendChat,
    textarea: screen.getByPlaceholderText(/ask the assistant/i) as HTMLTextAreaElement,
  }
}

const available = {
  available: true, reason: '', listening: false, session_id: null,
  model: 'medium-streaming', language: 'en', vendored: false,
}

function emit(event: string, detail: Record<string, unknown>) {
  act(() => { window.dispatchEvent(new CustomEvent(event, { detail })) })
}

beforeEach(() => {
  dictationStatus.mockReset()
  startDictation.mockReset()
  stopDictation.mockReset()
  startDictation.mockResolvedValue({ ok: true, session_id: 's1' })
  stopDictation.mockResolvedValue({ ok: true, session_id: 's1' })
  try { localStorage.removeItem('runspec:composerDraft') } catch { /* jsdom */ }
})

describe('CommandInput dictation', () => {
  it('hides the mic button when the backend reports dictation unavailable', async () => {
    dictationStatus.mockResolvedValue({ ...available, available: false, reason: 'not installed' })
    renderComposer()
    await waitFor(() => expect(dictationStatus).toHaveBeenCalled())
    expect(screen.queryByLabelText('Start dictation')).toBeNull()
  })

  it('starts on click, shows the live line, appends finals, and stops on send', async () => {
    dictationStatus.mockResolvedValue(available)
    const { onSendChat, textarea } = renderComposer()
    const mic = await screen.findByLabelText('Start dictation')

    fireEvent.click(mic)
    await waitFor(() => expect(startDictation).toHaveBeenCalledTimes(1))

    emit('runspec:dictation_state', { session_id: 's1', state: 'loading', progress: 0.5 })
    expect(screen.getByTestId('dictation-strip').textContent).toContain('Opening speech model… 50%')

    emit('runspec:dictation_state', { session_id: 's1', state: 'listening' })
    expect(screen.getByLabelText('Stop dictation')).toBeTruthy()
    expect(screen.getByTestId('dictation-strip').textContent).toMatch(/Listening/)

    emit('runspec:dictation_partial', { session_id: 's1', text: 'check the over' })
    expect(screen.getByTestId('dictation-strip').textContent).toContain('check the over')

    emit('runspec:dictation_final', { session_id: 's1', text: 'check the overnight batch' })
    expect(textarea.value).toBe('check the overnight batch')
    // The live line clears once it has moved into the box.
    expect(screen.getByTestId('dictation-strip').textContent).not.toContain('check the over')

    emit('runspec:dictation_final', { session_id: 's1', text: 'on db-3 and summarise' })
    expect(textarea.value).toBe('check the overnight batch on db-3 and summarise')

    // An event from some other session is ignored.
    emit('runspec:dictation_final', { session_id: 'stale', text: 'ghost' })
    expect(textarea.value).toBe('check the overnight batch on db-3 and summarise')

    // Enter sends the dictated text and ends dictation — nothing auto-sent before.
    expect(onSendChat).not.toHaveBeenCalled()
    fireEvent.keyDown(textarea, { key: 'Enter' })
    expect(onSendChat).toHaveBeenCalledWith('check the overnight batch on db-3 and summarise')
    expect(stopDictation).toHaveBeenCalled()

    emit('runspec:dictation_state', { session_id: 's1', state: 'ended', reason: 'stopped' })
    expect(screen.getByLabelText('Start dictation')).toBeTruthy()
    expect(screen.queryByTestId('dictation-strip')).toBeNull()
  })

  it('Ctrl+M toggles dictation from the keyboard', async () => {
    dictationStatus.mockResolvedValue(available)
    const { textarea } = renderComposer()
    await screen.findByLabelText('Start dictation')

    fireEvent.keyDown(textarea, { key: 'm', ctrlKey: true })
    await waitFor(() => expect(startDictation).toHaveBeenCalledTimes(1))
    emit('runspec:dictation_state', { session_id: 's1', state: 'listening' })

    fireEvent.keyDown(textarea, { key: 'm', ctrlKey: true })
    await waitFor(() => expect(stopDictation).toHaveBeenCalledTimes(1))
  })

  it('returns to idle when the backend refuses to start', async () => {
    dictationStatus.mockResolvedValue(available)
    startDictation.mockResolvedValue({ ok: false, error: 'no microphone' })
    renderComposer()
    fireEvent.click(await screen.findByLabelText('Start dictation'))
    await waitFor(() => expect(startDictation).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByLabelText('Start dictation')).toBeTruthy())
    expect(screen.queryByTestId('dictation-strip')).toBeNull()
  })
})

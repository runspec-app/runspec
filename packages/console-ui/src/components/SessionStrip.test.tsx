/**
 * SessionStrip: one chip per session with its kind, label, status affordance
 * (spinner / amber bell / unread dot), selected styling, and a close × that is
 * immediate for finished automation but confirmed for a chat with blocks.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

import { SessionStrip } from './SessionStrip'
import type { Session, SessionStatus } from '../sessions'

const sess = (over: Partial<Session>): Session => ({
  id: 'x', kind: 'chat', label: 'x', interactive: true, createdAt: 1, state: 'idle', unread: false, ...over,
})

function renderStrip(sessions: Session[], statuses: Record<string, SessionStatus>, over: Partial<React.ComponentProps<typeof SessionStrip>> = {}) {
  const onSelect = vi.fn()
  const onClose = vi.fn()
  render(
    <SessionStrip
      sessions={sessions}
      selectedId={over.selectedId ?? sessions[0]?.id ?? null}
      statusOf={s => statuses[s.id] ?? 'idle'}
      hasBlocks={over.hasBlocks ?? (() => false)}
      onSelect={onSelect}
      onClose={onClose}
    />,
  )
  return { onSelect, onClose }
}

const chip = (id: string) =>
  (screen.getAllByTestId('session-chip') as HTMLElement[]).find(c => c.dataset.sessionId === id) as HTMLElement

describe('SessionStrip', () => {
  it('renders nothing with no sessions', () => {
    const { container } = render(
      <SessionStrip sessions={[]} selectedId={null} statusOf={() => 'idle'} hasBlocks={() => false} onSelect={() => {}} onClose={() => {}} />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('shows each session with its label, status and selection', () => {
    renderStrip(
      [
        sess({ id: 'c1', label: 'restart nginx' }),
        sess({ id: 't1', kind: 'trigger', interactive: false, label: 'Trigger nightly · new_mail', state: 'running' }),
        sess({ id: 's1', kind: 'schedule', interactive: false, label: 'backup · runnable', state: 'running', unread: true }),
      ],
      { c1: 'idle', t1: 'running', s1: 'waiting' },
    )
    expect(chip('c1').dataset.selected).toBe('true')
    expect(chip('c1').textContent).toContain('restart nginx')
    expect(chip('t1').dataset.status).toBe('running')
    expect(chip('s1').dataset.status).toBe('waiting')
    expect(screen.getByLabelText('waiting for input')).toBeTruthy()
    expect(screen.getByLabelText('unread')).toBeTruthy()
    // An unlabelled chat reads as "New chat"; a running session has no close ×.
    expect(screen.queryAllByLabelText('Close session')).toHaveLength(1)
  })

  it('selects on click and closes a finished automation session immediately', () => {
    const { onSelect, onClose } = renderStrip(
      [sess({ id: 'c1', label: 'chat' }), sess({ id: 't1', kind: 'trigger', interactive: false, label: 'done one', state: 'done' })],
      { c1: 'idle', t1: 'done' },
      { hasBlocks: () => true },
    )
    fireEvent.click(chip('t1'))
    expect(onSelect).toHaveBeenCalledWith('t1')
    const closes = screen.getAllByLabelText('Close session')
    // The automation chip's × (second chip) closes at once, without selecting.
    onSelect.mockClear()
    fireEvent.click(closes[1])
    expect(onClose).toHaveBeenCalledWith('t1')
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('asks before closing a chat that has blocks', async () => {
    const { onClose } = renderStrip(
      [sess({ id: 'c1', label: 'chat' })],
      { c1: 'idle' },
      { hasBlocks: () => true },
    )
    await act(async () => { fireEvent.click(screen.getByLabelText('Close session')) })
    expect(onClose).not.toHaveBeenCalled()
    const ok = await screen.findByRole('button', { name: 'Close' })
    await act(async () => { fireEvent.click(ok) })
    expect(onClose).toHaveBeenCalledWith('c1')
  })
})

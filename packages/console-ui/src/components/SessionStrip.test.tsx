/**
 * SessionStrip: one chip per session with its kind, label, status affordance
 * (spinner / amber bell / unread dot), selected styling, and a close × that is
 * immediate for finished automation but confirmed for a chat with blocks.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

import { SessionStrip, partitionSessions, VISIBLE_AUTOMATION } from './SessionStrip'
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

describe('SessionStrip overflow', () => {
  // A burst of trigger runs (an over-active mail trigger) — newest last.
  const burst = (n: number, state: Session['state'] = 'done') =>
    Array.from({ length: n }, (_, i) =>
      sess({ id: `t${i}`, kind: 'trigger', interactive: false, label: `Trigger mail · ${i}`, createdAt: 100 + i, state }))

  it('keeps chats, the selection and waiting runs inline; newest automation fills the rest', () => {
    const list = [sess({ id: 'c1' }), ...burst(10)]
    const status = (s: Session): SessionStatus => (s.id === 't0' ? 'waiting' : s.interactive ? 'idle' : 'done')
    const { inline, overflow } = partitionSessions(list, status, 't1')
    const ids = inline.map(s => s.id)
    expect(ids).toContain('c1')
    expect(ids).toContain('t0') // waiting — never hidden in a menu
    expect(ids).toContain('t1') // selected
    // Plus the newest VISIBLE_AUTOMATION of the rest, in the list's order.
    expect(ids.filter(id => !['c1', 't0', 't1'].includes(id))).toEqual(['t6', 't7', 't8', 't9'])
    expect(overflow.map(s => s.id)).toEqual(['t5', 't4', 't3', 't2'])
    expect(inline.length + overflow.length).toBe(list.length)
  })

  it('shows a +N more menu that selects a run or closes all finished ones', async () => {
    const list = [sess({ id: 'c1' }), ...burst(VISIBLE_AUTOMATION + 3)]
    const { onSelect, onClose } = renderStrip(list, {}, { selectedId: 'c1' })
    expect(screen.getAllByTestId('session-chip')).toHaveLength(1 + VISIBLE_AUTOMATION)
    const more = screen.getByTestId('session-overflow')
    expect(more.textContent).toContain('+3 more')

    await act(async () => { fireEvent.click(more) })
    await act(async () => { fireEvent.click(await screen.findByText('Trigger mail · 1')) })
    expect(onSelect).toHaveBeenCalledWith('t1')

    await act(async () => { fireEvent.click(more) })
    await act(async () => { fireEvent.click(await screen.findByText('Close 3 finished')) })
    expect(onClose.mock.calls.map(c => c[0]).sort()).toEqual(['t0', 't1', 't2'])
  })

  it('shows no overflow control when everything fits', () => {
    renderStrip([sess({ id: 'c1' }), ...burst(VISIBLE_AUTOMATION)], {})
    expect(screen.queryByTestId('session-overflow')).toBeNull()
  })
})

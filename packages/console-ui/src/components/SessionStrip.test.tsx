/**
 * SessionStrip: one chip per session with its kind, label, status affordance
 * (spinner / amber bell / unread dot), selected styling, and a close × that is
 * immediate for finished automation but confirmed for a chat with blocks.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

import { SessionStrip, chipText, partitionSessions, VISIBLE_AUTOMATION } from './SessionStrip'
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
    // Waiting is said in words; the chip keeps its place in the row.
    expect(chip('s1').textContent).toContain('waiting for you')
    expect(screen.getAllByTestId('session-chip')[0].dataset.sessionId).not.toBe('s1')
    expect(screen.getByLabelText('unread')).toBeTruthy()
    // A running session has no close ×.
    expect(screen.queryAllByLabelText(/^Close /)).toHaveLength(1)
  })

  it('is one keyboard stop: Tab lands on the selected chip, arrows move, Enter opens', () => {
    const { onSelect } = renderStrip(
      [sess({ id: 'c1', label: 'chat' }), sess({ id: 'w1', kind: 'trigger', interactive: false, label: 'waiting one', state: 'running' })],
      { c1: 'idle', w1: 'waiting' },
    )
    expect(chip('c1').tabIndex).toBe(0)
    expect(chip('w1').tabIndex).toBe(-1)
    chip('c1').focus()
    fireEvent.keyDown(chip('c1'), { key: 'ArrowRight' })
    expect(document.activeElement).toBe(chip('w1'))
    fireEvent.keyDown(chip('w1'), { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith('w1')
    fireEvent.keyDown(chip('w1'), { key: 'ArrowRight' })
    expect(document.activeElement).toBe(chip('c1'))
  })

  it('selects on click and closes a finished automation session immediately', () => {
    const { onSelect, onClose } = renderStrip(
      [sess({ id: 'c1', label: 'chat' }), sess({ id: 't1', kind: 'trigger', interactive: false, label: 'done one', state: 'done' })],
      { c1: 'idle', t1: 'done' },
      { hasBlocks: () => true },
    )
    fireEvent.click(chip('t1'))
    expect(onSelect).toHaveBeenCalledWith('t1')
    // Each × names the chat it closes.
    const closes = screen.getAllByLabelText(/^Close /)
    expect(closes[1].getAttribute('aria-label')).toMatch(/^Close done one/)
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
    await act(async () => { fireEvent.click(screen.getByLabelText(/^Close /)) })
    expect(onClose).not.toHaveBeenCalled()
    await act(async () => { fireEvent.click(await screen.findByTestId('risk-ok')) })
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

describe('chipText', () => {
  it('adds the start time to an automation label that has none, and leaves chats alone', () => {
    const at = new Date(2026, 10, 13, 9, 5).getTime()
    expect(chipText(sess({ kind: 'trigger', interactive: false, label: 'Trigger x · new_mail', createdAt: at }))).toBe('Trigger x · new_mail · 09:05')
    expect(chipText(sess({ kind: 'trigger', interactive: false, label: 'x · 09:05 · subject', createdAt: at }))).toBe('x · 09:05 · subject')
    expect(chipText(sess({ label: 'restart nginx', createdAt: at }))).toBe('restart nginx')
  })
})

describe('chipText', () => {
  it('names a fresh chat by the time it opened, then by its first message', async () => {
    const { chipText } = await import('./SessionStrip')
    const at = new Date(2026, 10, 15, 14, 5).getTime()
    expect(chipText(sess({ id: 'n', label: '', createdAt: at }))).toBe('New · 14:05')
    expect(chipText(sess({ id: 'n', label: 'restart nginx on web-01', createdAt: at }))).toBe('restart nginx on web-01')
  })
})

describe('wholeChipScroll()', () => {
  // Five 100px chips, 6px apart: starts 0, 106, 212, 318, 424.
  const starts = [0, 106, 212, 318, 424]
  const widths = [100, 100, 100, 100, 100]
  it('shows the target in full and starts on a whole chip', async () => {
    const { wholeChipScroll } = await import('./SessionStrip')
    // 250px view, target the last chip: its right edge 524 needs >= 274; the
    // first whole chip from there starts at 318.
    expect(wholeChipScroll(starts, widths, 250, 0, 4)).toBe(318)
    // Already in view at a chip start: stays.
    expect(wholeChipScroll(starts, widths, 250, 106, 1)).toBe(106)
    // Scrolled to a sliver (60): moves to the next whole chip that keeps the target.
    expect(wholeChipScroll(starts, widths, 250, 60, 2)).toBe(106)
    // Target left of view: starts at it.
    expect(wholeChipScroll(starts, widths, 250, 318, 0)).toBe(0)
  })
})

describe('chipEndPad()', () => {
  it('adds room after the last chip only when the whole-chip position is out of reach', async () => {
    const { chipEndPad } = await import('./SessionStrip')
    // 524px of chips in a 250px view scroll at most 274: reaching 318 needs 44px more.
    expect(chipEndPad(318, 524, 250)).toBe(44)
    expect(chipEndPad(106, 524, 250)).toBe(0)
  })
})

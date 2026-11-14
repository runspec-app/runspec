/**
 * The shared approval card pieces: every waiting card knows its place in the
 * queue, and only the first (the one Alt+A answers) carries the key chip.
 */
import { describe, it, expect, vi } from 'vitest'
import { useRef } from 'react'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'

import { ApprovalActions, QueueMark, gatePosition, useGatePosition } from './approval'
import { answerPendingConfirm } from '../shortcuts'

function Gate({ name, onApprove = () => {} }: { name: string; onApprove?: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const q = useGatePosition(ref)
  return (
    <div ref={ref} data-rs-confirm-gate="" data-testid={`gate-${name}`}>
      <QueueMark {...q} />
      <button data-rs-confirm="approve" onClick={onApprove}>approve {name}</button>
    </div>
  )
}

describe('approval queue', () => {
  it('numbers the waiting cards in keyboard order and marks the first', async () => {
    render(<div><Gate name="webhook" /><Gate name="agent" /></div>)
    await waitFor(() => expect(screen.getAllByTestId('approval-queue')[0].textContent).toContain('1 of 2 waiting'))
    expect(screen.getAllByTestId('approval-queue')[1].textContent).toContain('2 of 2 waiting')
    expect(screen.getAllByTestId('approval-keys')).toHaveLength(1)
    expect(screen.getByTestId('gate-webhook').contains(screen.getByTestId('approval-keys'))).toBe(true)
    expect(gatePosition(screen.getByTestId('gate-agent'))).toEqual({ index: 1, total: 2 })
  })

  it('says nothing about a queue when only one card waits', async () => {
    render(<Gate name="only" />)
    await waitFor(() => expect(screen.getByTestId('approval-keys')).toBeTruthy())
    expect(screen.getByTestId('approval-queue').textContent).not.toContain('waiting')
  })

  it('the keyboard answers the card with the chip', () => {
    const first = vi.fn(), second = vi.fn()
    render(<div><Gate name="webhook" onApprove={first} /><Gate name="agent" onApprove={second} /></div>)
    expect(answerPendingConfirm('approve', { inView: () => true })).toBe('answered')
    expect(first).toHaveBeenCalled()
    expect(second).not.toHaveBeenCalled()
  })
})

describe('ApprovalActions', () => {
  it('opens a reason on request and denies with it on Enter', () => {
    const onDeny = vi.fn()
    let reason = ''
    const { rerender } = render(<ApprovalActions verb="run" onApprove={() => {}} onDeny={onDeny} reason={reason}
      setReason={s => { reason = s }} reasonHint="Recorded." />)
    expect(screen.getByRole('button', { name: /Approve & run/ })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Add a reason…' }))
    fireEvent.change(screen.getByLabelText('Reason (optional)'), { target: { value: 'not now' } })
    rerender(<ApprovalActions verb="run" onApprove={() => {}} onDeny={onDeny} reason={reason}
      setReason={s => { reason = s }} reasonHint="Recorded." />)
    expect(screen.getByRole('button', { name: /Deny with reason/ })).toBeTruthy()
    fireEvent.keyDown(screen.getByLabelText('Reason (optional)'), { key: 'Enter' })
    expect(onDeny).toHaveBeenCalled()
  })
})

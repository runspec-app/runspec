import { describe, it, expect } from 'vitest'
import { useEffect } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { confirmWord, targetList, useRiskConfirm, type RiskAnswer, type RiskAsk } from './risk'

function Harness({ ask, onAnswer }: { ask: RiskAsk; onAnswer: (a: RiskAnswer) => void }) {
  const [confirm, holder] = useRiskConfirm()
  useEffect(() => { void confirm(ask).then(onAnswer) }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  return <>{holder}</>
}

describe('risk ladder', () => {
  it('lists targets, eliding past twelve', () => {
    expect(targetList(['a', 'b'])).toBe('a, b')
    expect(targetList(Array.from({ length: 14 }, (_, i) => `h${i}`))).toMatch(/h11 and 2 more$/)
  })

  it('types the target count unless told otherwise', () => {
    expect(confirmWord({ level: 'destructive', title: '', action: '', okText: '', targets: ['a', 'b'] })).toBe('2')
    expect(confirmWord({ level: 'destructive', title: '', action: '', okText: '', typeToConfirm: 'ops' })).toBe('ops')
  })

  it('a fleet ask shows the action and targets, focuses Cancel, and can remember', async () => {
    let answer: RiskAnswer | null = null
    render(<Harness onAnswer={a => { answer = a }} ask={{
      level: 'fleet', title: 'Send?', action: 'uptime', targets: ['web-01', 'web-02'], okText: 'Send to 2', rememberLabel: "Don't ask again",
    }} />)
    expect((await screen.findByTestId('risk-action')).textContent).toBe('uptime')
    expect(screen.getByText('web-01, web-02')).toBeTruthy()
    await waitFor(() => expect(document.activeElement?.textContent).toBe('Cancel'))
    fireEvent.click(screen.getByRole('checkbox', { name: "Don't ask again" }))
    fireEvent.click(screen.getByRole('button', { name: 'Send to 2' }))
    await waitFor(() => expect(answer).toEqual({ ok: true, remember: true }))
  })

  it('a destructive ask keeps OK off until the word is typed', async () => {
    let answer: RiskAnswer | null = null
    render(<Harness onAnswer={a => { answer = a }} ask={{
      level: 'destructive', title: 'Delete?', action: 'Delete 5 files', typeToConfirm: 'ops', okText: 'Delete',
    }} />)
    const ok = await screen.findByTestId('risk-ok')
    expect(ok.hasAttribute('disabled')).toBe(true)
    fireEvent.change(screen.getByLabelText('Type ops to confirm'), { target: { value: 'op' } })
    expect(ok.hasAttribute('disabled')).toBe(true)
    fireEvent.change(screen.getByLabelText('Type ops to confirm'), { target: { value: 'ops' } })
    fireEvent.click(ok)
    await waitFor(() => expect(answer).toEqual({ ok: true, remember: false }))
  })
})

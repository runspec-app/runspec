import { describe, it, expect, beforeEach } from 'vitest'
import { useEffect } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { askRemembered, confirmWord, consentCount, consentKey, forgetConsents, hasConsent, targetList, useRiskConfirm, type RiskAnswer, type RiskAsk } from './risk'
import { savedRunTargets } from './runShared'

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
    // One target: its name, not "1".
    expect(confirmWord({ level: 'destructive', title: '', action: '', okText: '', targets: ['web-01'] })).toBe('web-01')
    expect(confirmWord({ level: 'destructive', title: '', action: '', okText: '' })).toBe('delete')
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

describe('remembered consent', () => {
  beforeEach(() => forgetConsents())
  const ASK: RiskAsk = { level: 'fleet', title: 'Run it?', action: 'x', targets: ['a', 'b'], okText: 'Run' }

  it('keys an action on a set of targets, order-independent', () => {
    expect(consentKey('run:x', ['b', 'a'])).toBe(consentKey('run:x', ['a', 'b']))
    expect(consentKey('run:x', ['a'])).not.toBe(consentKey('run:x', ['a', 'b']))
  })

  it('asks until a ticked OK, then skips for that key only', async () => {
    const key = consentKey('run:x', ['a', 'b'])
    let asked = 0
    const ask = async (a: RiskAsk): Promise<RiskAnswer> => { asked++; expect(a.rememberLabel).toBeTruthy(); return { ok: true, remember: asked === 2 } }
    expect(await askRemembered(ask, key, ASK)).toBe(true)
    expect(hasConsent(key)).toBe(false)
    expect(await askRemembered(ask, key, ASK)).toBe(true)
    expect(hasConsent(key)).toBe(true)
    expect(await askRemembered(ask, key, ASK)).toBe(true)
    expect(asked).toBe(2)
    expect(await askRemembered(ask, consentKey('run:x', ['a']), ASK)).toBe(true)
    expect(asked).toBe(3)
    expect(consentCount()).toBe(1)
    forgetConsents()
    expect(hasConsent(key)).toBe(false)
  })

  it('never remembers a destructive ask, and a Cancel is a no', async () => {
    const key = consentKey('del', ['a'])
    expect(await askRemembered(async a => { expect(a.rememberLabel).toBeUndefined(); return { ok: true, remember: true } }, key, { ...ASK, level: 'destructive' })).toBe(true)
    expect(hasConsent(key)).toBe(false)
    expect(await askRemembered(async () => ({ ok: false, remember: true }), key, ASK)).toBe(false)
    expect(hasConsent(key)).toBe(false)
  })

  it('names where a saved run goes', () => {
    const base = { host: '', group: '', hosts: [], selected_hosts: [], profile: '' }
    expect(savedRunTargets({ ...base, host: 'prod-1' })).toEqual(['prod-1'])
    expect(savedRunTargets({ ...base, group: 'web' })).toEqual(['group web'])
    expect(savedRunTargets({ ...base, selected_hosts: ['a', 'b'] })).toEqual(['a', 'b'])
    expect(savedRunTargets({ ...base, hosts: ['a', 'b'] })).toEqual(['first connected of a, b'])
    expect(savedRunTargets({ ...base, profile: 'prod' })).toEqual(['profile prod'])
  })
})

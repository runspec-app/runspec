import { describe, expect, it } from 'vitest'
import { offerDebugSwitch, withDebugFlag } from './formsShared'

describe('debug logging switch', () => {
  it('adds debug: true only when switched on', () => {
    const declared = new Set(['path'])
    expect(withDebugFlag({ path: '/tmp' }, true, declared)).toEqual({ path: '/tmp', debug: true })
    expect(withDebugFlag({ path: '/tmp' }, false, declared)).toEqual({ path: '/tmp' })
  })

  it('leaves a runnable that declares its own debug arg alone', () => {
    const declared = new Set(['debug'])
    expect(offerDebugSwitch(declared)).toBe(false)
    expect(withDebugFlag({ debug: 'verbose' }, true, declared)).toEqual({ debug: 'verbose' })
  })

  it('does not mutate the form values', () => {
    const values = { a: 1 }
    withDebugFlag(values, true, new Set())
    expect(values).toEqual({ a: 1 })
  })
})

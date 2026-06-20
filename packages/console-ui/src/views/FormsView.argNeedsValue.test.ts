import { describe, it, expect } from 'vitest'
import { argNeedsValue } from './FormsView'
import type { ArgDef } from '../bridge'

const arg = (over: Partial<ArgDef>): ArgDef => ({ name: 'x', type: 'str', required: false, ...over })

describe('argNeedsValue', () => {
  it('blocks a required non-secret arg when empty and not covered', () => {
    expect(argNeedsValue(arg({ type: 'str', required: true }), '', false)).toBe(true)
    expect(argNeedsValue(arg({ type: 'str', required: true }), undefined, false)).toBe(true)
  })

  it('does not block when a value is present', () => {
    expect(argNeedsValue(arg({ type: 'str', required: true }), 'v', false)).toBe(false)
  })

  it('does not block a required arg a credential covers', () => {
    expect(argNeedsValue(arg({ type: 'str', required: true }), '', true)).toBe(false)
  })

  it('never blocks a password arg — even required, empty, uncovered', () => {
    // The secret arrives out-of-band (credential / env / typed field); the form
    // can't validate it and must not stop the run.
    expect(argNeedsValue(arg({ name: 'password', type: 'password', required: true }), '', false)).toBe(false)
    expect(argNeedsValue(arg({ name: 'password', type: 'password', required: true }), undefined, false)).toBe(false)
  })

  it('does not block an optional arg', () => {
    expect(argNeedsValue(arg({ type: 'str', required: false }), '', false)).toBe(false)
  })
})

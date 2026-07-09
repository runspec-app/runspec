import { describe, it, expect } from 'vitest'
import { validateArgs } from './SchedulesView'
import type { ArgDef } from '../bridge'

const arg = (over: Partial<ArgDef>): ArgDef => ({ name: 'x', type: 'str', required: false, ...over })

describe('validateArgs (Schedules modal)', () => {
  it('flags a required arg when the value is missing or empty', () => {
    const args = [arg({ name: 'name', required: true })]
    expect(validateArgs(args, {})).toEqual({ name: 'Required' })
    expect(validateArgs(args, { name: '' })).toEqual({ name: 'Required' })
    expect(validateArgs(args, { name: 'v' })).toEqual({})
  })

  it('accepts a flag arg with a false value as satisfying required (a flag is boolean)', () => {
    // A flag arg's value is boolean; false is a legitimate value, not "missing".
    // (Realistically flags are never marked required, but the check should not
    // pretend that boolean false equals empty.)
    const args = [arg({ name: 'debug', type: 'flag', required: true })]
    // Empty state is still Required — nothing was set at all.
    expect(validateArgs(args, {})).toEqual({ debug: 'Required' })
    // But a set-to-false flag counts as a value (the row is rendered as a Switch,
    // not blank), so we don't complain.
    expect(validateArgs(args, { debug: false })).toEqual({})
    expect(validateArgs(args, { debug: true })).toEqual({})
  })

  it('checks numeric range for int/float args', () => {
    const args = [arg({ name: 'workers', type: 'int', range: [1, 10] })]
    expect(validateArgs(args, { workers: 5 })).toEqual({})
    expect(validateArgs(args, { workers: 0 })).toEqual({ workers: 'Must be between 1 and 10' })
    expect(validateArgs(args, { workers: 11 })).toEqual({ workers: 'Must be between 1 and 10' })
  })

  it('skips the range check when the value is absent (already covered by required)', () => {
    const args = [arg({ name: 'workers', type: 'int', range: [1, 10] })]
    expect(validateArgs(args, {})).toEqual({})
    expect(validateArgs(args, { workers: '' })).toEqual({})
  })
})

import { describe, it, expect } from 'vitest'
import { needState, suggestKeys } from './HostScheduleEnv'
import type { ArgDef, HostScheduleEnvNeed } from '../bridge'

const need = (over: Partial<HostScheduleEnvNeed> = {}): HostScheduleEnvNeed => ({
  arg: 'token', command: '', password: true, secret: true, description: '',
  keys: ['RUNSPEC_X_ARG_TOKEN', 'X_TOKEN'], set_by: '', blank: [], ...over,
})
const arg = (over: Partial<ArgDef> = {}): ArgDef => ({ name: 'token', type: 'password', required: true, ...over })

describe('needState', () => {
  it('is covered by the file when a key has a value', () => {
    expect(needState(need({ set_by: 'X_TOKEN' }), arg(), {})).toBe('file')
  })
  it('counts a schedule value only for the user half of a pair', () => {
    const user = need({ arg: 'user', password: false, secret: false, keys: ['X_USER'] })
    expect(needState(user, arg({ name: 'user', type: 'str' }), { user: 'svc' })).toBe('schedule')
    // A secret typed into the schedule would sit in its config: still missing.
    expect(needState(need({ password: false }), arg({ type: 'str' }), { token: 'abc' })).toBe('missing')
  })
  it('is optional when the arg is not required or has a default', () => {
    expect(needState(need(), arg({ required: false }), {})).toBe('optional')
    expect(needState(need(), arg({ default: '' }), {})).toBe('optional')
    expect(needState(need(), arg(), {})).toBe('missing')
  })
})

describe('suggestKeys', () => {
  it('puts declared aliases before the automatic name', () => {
    expect(suggestKeys(need())).toEqual(['X_TOKEN', 'RUNSPEC_X_ARG_TOKEN'])
  })
})

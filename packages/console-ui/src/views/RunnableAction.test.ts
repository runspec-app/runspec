import { describe, it, expect } from 'vitest'
import {
  argDefToParamDef, runnableSelectOptions, runnablesForTarget, splitFixedAndOpen,
} from './RunnableAction'
import type { ArgDef, Host, Runnable } from '../bridge'

const host = (name: string, group = '', connected = true): Host =>
  ({ name, group, connected } as unknown as Host)

const runnable = (name: string, on: string): Runnable =>
  ({ name, host: on, group: 'tools', args: [] } as unknown as Runnable)

const HOSTS = [host('w1', 'Prod'), host('w2', 'Prod'), host('db1', 'Data')]
const RUNNABLES = [
  runnable('restart', 'w1'), runnable('restart', 'w2'),
  runnable('backup', 'db1'), runnable('df', 'w2'),
]

const arg = (over: Partial<ArgDef>): ArgDef => ({ name: 'x', type: 'str', required: false, ...over })

describe('runnablesForTarget', () => {
  it('single host mode returns that host’s runnables', () => {
    const out = runnablesForTarget(RUNNABLES, HOSTS, 'host', { host: 'w2' })
    expect(out.map(r => r.name).sort()).toEqual(['df', 'restart'])
  })

  it('group mode de-duplicates by name across members', () => {
    const out = runnablesForTarget(RUNNABLES, HOSTS, 'group', { group: 'Prod' })
    expect(out.map(r => r.name).sort()).toEqual(['df', 'restart'])
    expect(out.filter(r => r.name === 'restart')).toHaveLength(1)
  })

  it('anyhost / selected modes use the picked lists', () => {
    expect(
      runnablesForTarget(RUNNABLES, HOSTS, 'anyhost', { hostsList: ['db1'] }).map(r => r.name),
    ).toEqual(['backup'])
    expect(
      runnablesForTarget(RUNNABLES, HOSTS, 'selected', { selectedList: ['w1', 'db1'] })
        .map(r => r.name).sort(),
    ).toEqual(['backup', 'restart'])
  })

  it('empty selection yields nothing', () => {
    expect(runnablesForTarget(RUNNABLES, HOSTS, 'host', {})).toEqual([])
    expect(runnablesForTarget(RUNNABLES, HOSTS, 'group', { group: 'Nope' })).toEqual([])
  })
})

describe('runnableSelectOptions', () => {
  // Regression guard for the 0.191.0 empty-dropdown bug: the runnable picker's
  // options MUST be plain `{value, label}` data for the Select `options` prop —
  // antd never renders a custom component child to discover <Select.Option>s.
  it('returns plain options data with the runnable name as the value', () => {
    const out = runnableSelectOptions([runnable('restart', 'w1')], {})
    expect(out).toHaveLength(1)
    expect(out[0].value).toBe('restart')
    expect(out[0].label).toBeTruthy()
  })

  it('prepends an undiscovered stored runnable so editing never loses it', () => {
    const out = runnableSelectOptions([runnable('restart', 'w1')], {}, 'gone-away')
    expect(out.map(o => o.value)).toEqual(['gone-away', 'restart'])
  })

  it('does not duplicate a fallback that is discovered', () => {
    const out = runnableSelectOptions([runnable('restart', 'w1')], {}, 'restart')
    expect(out.map(o => o.value)).toEqual(['restart'])
  })
})

describe('argDefToParamDef', () => {
  it('carries type, choices, required, description, default, range, and ui hint', () => {
    const def = argDefToParamDef(arg({
      name: 'mode', type: 'choice', options: ['fast', 'slow'], required: true,
      description: 'How fast', default: 'fast',
    }))
    expect(def).toEqual({
      type: 'choice', options: ['fast', 'slow'], required: true,
      description: 'How fast', default: 'fast',
    })
    expect(argDefToParamDef(arg({ type: 'int', range: [1, 10] }))).toEqual({
      type: 'int', range: [1, 10],
    })
    expect(argDefToParamDef(arg({ type: 'str', ui: 'date' }))).toEqual({
      type: 'str', ui: 'date',
    })
  })

  it('keeps a password arg password-typed (asked at run time, secret env channel)', () => {
    expect(argDefToParamDef(arg({ type: 'password', required: true }))).toEqual({
      type: 'password', required: true,
    })
  })
})

describe('splitFixedAndOpen', () => {
  const declared = [
    arg({ name: 'service', required: true }),
    arg({ name: 'reason', required: true }),
    arg({ name: 'workers', type: 'int', range: [1, 8] }),
    arg({ name: 'token', type: 'password' }),
  ]

  it('typed values become fixed args; Open args become generated params', () => {
    const out = splitFixedAndOpen(declared, { service: 'nginx', workers: 4 }, new Set(['reason']))
    expect(out.errors).toEqual({})
    expect(out.args).toEqual({ service: 'nginx', workers: 4 })
    expect(Object.keys(out.params).sort()).toEqual(['reason', 'token'])
    expect(out.params.reason).toEqual({ type: 'str', required: true })
  })

  it('a password arg is always open, never a fixed arg (synced file, no secrets)', () => {
    const out = splitFixedAndOpen(declared, { service: 'x', reason: 'y', token: 'hunter2' }, new Set())
    expect(out.args).not.toHaveProperty('token')
    expect(out.params).toHaveProperty('token')
  })

  it('a required arg must be fixed or Open', () => {
    const out = splitFixedAndOpen(declared, { service: 'nginx' }, new Set())
    expect(out.errors.reason).toMatch(/Required/)
  })

  it('a fixed numeric value must sit inside the range', () => {
    const out = splitFixedAndOpen(declared, { service: 'a', reason: 'b', workers: 99 }, new Set())
    expect(out.errors.workers).toMatch(/between 1 and 8/)
    expect(out.args).not.toHaveProperty('workers')
  })

  it('blank optional values are simply omitted', () => {
    const out = splitFixedAndOpen(declared, { service: 'a', reason: 'b', workers: '' }, new Set())
    expect(out.errors).toEqual({})
    expect(out.args).toEqual({ service: 'a', reason: 'b' })
  })
})

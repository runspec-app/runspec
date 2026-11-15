import { describe, it, expect } from 'vitest'
import { denyAppliedEverywhere } from './denylistPending'

describe('denyAppliedEverywhere', () => {
  it('is true only when every venv tried said ok', () => {
    expect(denyAppliedEverywhere('runspec deny all — 2/2 venv(s) ok across 1 host(s):\n✓ a')).toBe(true)
    expect(denyAppliedEverywhere('runspec deny all — 1/2 venv(s) ok across 1 host(s):')).toBe(false)
    expect(denyAppliedEverywhere('runspec deny all — 0/0 venv(s) ok across 0 host(s):')).toBe(false)
    expect(denyAppliedEverywhere('error: unreachable')).toBe(false)
  })
})

describe('schedulerBlocked', () => {
  it('names the daemons an enforcing denylist would keep down', async () => {
    const { schedulerBlocked } = await import('./denylistPending')
    const ds = [{ host: 'a', venv: 'ops' }, { host: 'b', venv: 'jobs' }, { host: 'c', venv: 'x' }]
    expect(schedulerBlocked([
      { host: 'a', venv: 'ops', ok: true, mode: 'enforce', disabled: true },
      { host: 'b', venv: 'jobs', ok: true, mode: 'enforce', rules: [{ runnables: ['runspec-*'] }] },
      { host: 'c', venv: 'x', ok: true, mode: 'enforce', rules: [{ runnables: ['deploy'] }] },
    ], ds)).toEqual(['a / ops', 'b / jobs'])
    // Warn or off doesn't refuse; an unread row doesn't count.
    expect(schedulerBlocked([{ host: 'a', venv: 'ops', ok: true, mode: 'warn', disabled: true }], ds)).toEqual([])
    expect(schedulerBlocked([{ host: 'a', venv: 'ops', ok: false, disabled: true }], ds)).toEqual([])
  })
})

/** The Schedules tab remembers which option card the operator last picked
 *  (All / This console / Host cron / Host daemon), and reads host schedules from
 *  local plus the connected hosts of the active profile. */
import { describe, it, expect, beforeEach } from 'vitest'
import { readScheduleScope, scheduleTargets } from './SchedulesView'
import type { Host } from '../bridge'

describe('readScheduleScope', () => {
  beforeEach(() => localStorage.clear())
  it('defaults to All', () => expect(readScheduleScope()).toBe('all'))
  it('restores each card', () => {
    for (const v of ['all', 'console', 'cron', 'daemon'] as const) {
      localStorage.setItem('runspec.schedules.scope', v)
      expect(readScheduleScope()).toBe(v)
    }
  })
  it('maps the old Shared (host cron) choice to Host cron', () => {
    localStorage.setItem('runspec.schedules.scope', 'shared')
    expect(readScheduleScope()).toBe('cron')
  })
  it('ignores junk', () => {
    localStorage.setItem('runspec.schedules.scope', 'nonsense')
    expect(readScheduleScope()).toBe('all')
  })
})

describe('scheduleTargets', () => {
  const h = (name: string, connected: boolean): Host => ({ name, connected, runnableCount: 0, groups: [] })
  it('is local first, then connected hosts only', () => {
    expect(scheduleTargets([h('web', true), h('local', true), h('db', false), h('api', true)])).toEqual(['local', 'web', 'api'])
  })
  it('always includes local', () => expect(scheduleTargets([])).toEqual(['local']))
})

/** The Schedules tab remembers which scope the operator last viewed — this
 *  console's own schedules, or a host's shared cron schedules. */
import { describe, it, expect, beforeEach } from 'vitest'
import { readScheduleScope } from './SchedulesView'

describe('readScheduleScope', () => {
  beforeEach(() => localStorage.clear())
  it('defaults to this console', () => expect(readScheduleScope()).toBe('console'))
  it('restores shared', () => {
    localStorage.setItem('runspec.schedules.scope', 'shared')
    expect(readScheduleScope()).toBe('shared')
  })
  it('ignores junk', () => {
    localStorage.setItem('runspec.schedules.scope', 'nonsense')
    expect(readScheduleScope()).toBe('console')
  })
})

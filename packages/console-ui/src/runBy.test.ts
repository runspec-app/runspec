import { describe, it, expect } from 'vitest'
import { isScheduled, runBy } from './runBy'

describe('runBy', () => {
  it('names the backend that fired a host schedule, not the agent flag', () => {
    expect(runBy({ initiatedBy: 'llm', trigger: 'schedule:cron', label: 'sched:nightly' }).label).toBe('cron')
    expect(runBy({ initiatedBy: 'llm', trigger: 'schedule:daemon', label: 'sched:nightly' }).kind).toBe('daemon')
    expect(runBy({ initiatedBy: 'llm', trigger: 'schedule:run-now', label: 'sched:nightly' }).kind).toBe('run-now')
    // fired before runspec-scheduler 0.7.0: only the label says it was a host schedule
    expect(runBy({ initiatedBy: 'llm', trigger: 'schedule', label: 'sched:nightly' }).kind).toBe('host-schedule')
  })

  it('keeps agent and user for everything else', () => {
    expect(runBy({ initiatedBy: 'llm', trigger: 'schedule', label: 'console' }).kind).toBe('agent')
    expect(runBy({ initiatedBy: 'user', trigger: 'schedule', label: 'console' }).kind).toBe('user')
    expect(runBy({}).kind).toBe('user')
  })

  it('treats every schedule trigger as scheduled', () => {
    expect(['schedule', 'schedule:cron', 'schedule:daemon', 'schedule:run-now'].every(isScheduled)).toBe(true)
    expect(isScheduled('mail')).toBe(false)
    expect(isScheduled(undefined)).toBe(false)
  })
})

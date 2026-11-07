/**
 * sharedSchedules: the relative-time label and the expandable
 * detail of a host schedule (recent runs, captured output, daemon settings).
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import { ScheduleDetail, relTime } from './sharedSchedules'
import type { SharedSchedule } from '../bridge'

const base: SharedSchedule = {
  id: 'nightly', runnable: 'backup', enabled: true, when: { cron: '0 2 * * *' }, args: { target: 'prod' },
  run_as: 'svc', installable: true, not_installable_reason: null, installed: true, line: 'exec',
  next_run: null, runs: [], last_output: null,
}

describe('ScheduleDetail', () => {
  it('shows recent runs and the captured output', () => {
    render(<ScheduleDetail s={{
      ...base,
      runs: [{ run_id: 'r1', status: 'failed', exit_code: 2, start_ts: new Date().toISOString(), duration_ms: 1500, trigger: 'schedule' }],
      last_output: { stdout: 'copied 3 files\n', stderr: 'disk full\n', truncated: false, errors: ['upload failed'], result: { message: 'backup failed — exit 2 in 1.50s' } },
    }} />)
    expect(screen.getByText('exit 2')).toBeTruthy()
    expect(screen.getByText('copied 3 files')).toBeTruthy()
    expect(screen.getByText('disk full')).toBeTruthy()
    expect(screen.getByText('upload failed')).toBeTruthy()
  })

  it('names a legacy line when nothing was recorded', () => {
    render(<ScheduleDetail s={{ ...base, line: 'legacy' }} />)
    expect(screen.getByText(/legacy crontab line doesn’t record fires/)).toBeTruthy()
  })

  it('shows catch-up and timeout for a daemon schedule', () => {
    render(<ScheduleDetail s={{ ...base, line: 'daemon', catch_up: true, timeout: null }} daemon />)
    expect(screen.getByText(/run once when the scheduler starts/)).toBeTruthy()
    expect(screen.getByText(/6h \(default\)/)).toBeTruthy()
  })
})

describe('helpers', () => {
  it('relTime', () => {
    const now = Date.parse('2026-09-28T12:00:00Z')
    expect(relTime('2026-09-28T11:00:00Z', now)).toBe('1h ago')
    expect(relTime('2026-09-28T12:05:00Z', now)).toBe('in 5m')
    expect(relTime(null, now)).toBe('—')
  })
})

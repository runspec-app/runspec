/**
 * allSchedules: rows from console schedules + each venv's runspec-scheduler
 * status (cron or daemon backend), one state per row, card counts, filters, sort.
 */
import { describe, it, expect } from 'vitest'
import {
  DEFAULT_FILTERS, collectRows, filterRows, inScope, scopeCounts, sharedState, timeoutLabel, shortDuration,
} from './allSchedules'
import type { RunCard, Schedule, SharedSchedule, SharedSchedulesVenv } from '../bridge'

const sched = (over: Partial<SharedSchedule> = {}): SharedSchedule => ({
  id: 'nightly', runnable: 'backup', enabled: true, when: { cron: '0 2 * * *' }, args: { target: 'prod' },
  run_as: 'svc', installable: true, not_installable_reason: null, installed: true, line: 'exec',
  next_run: '2026-11-08T02:00:00Z', runs: [], last_output: null, ...over,
})

const cronVenv = (schedules: SharedSchedule[], over: Partial<SharedSchedulesVenv> = {}): SharedSchedulesVenv => ({
  host: 'web-1', group: 'ops', available: true, ok: true, owner: 'svc',
  status: { ok: true, venv: 'ops', config: 'c', schedules, stale: [], errors: [] },  // no backend key = older scheduler = cron
  ...over,
})

const daemonVenv = (schedules: SharedSchedule[], state: 'running' | 'stale' | 'stopped' | null, extra: Record<string, unknown> = {}): SharedSchedulesVenv => ({
  host: 'job-1', group: 'jobs', available: true, ok: true, owner: 'svc-jobs',
  status: {
    ok: true, venv: 'jobs', config: 'c', backend: 'daemon',
    daemon: state ? { state, pid: 42, uptime_s: 7200, heartbeat_age_s: 5, ...extra } : null,
    schedules, stale: [], errors: [],
  },
})

const consoleSched = (over: Partial<Schedule> = {}): Schedule => ({
  id: 'rs-1', action: 'runnable', runnable: 'disk-usage', host: 'web-1', args: { path: '/' },
  when: { cron: '0 * * * *' }, enabled: true, nextRun: '2026-11-07T08:00:00Z', ...over,
} as Schedule)

describe('sharedState', () => {
  const v = cronVenv([])
  it('orders the cron states', () => {
    expect(sharedState(sched({ enabled: false, installed: false }), v).state).toBe('disabled')
    expect(sharedState(sched({ installable: false, not_installable_reason: 'every' }), v)).toMatchObject({ state: 'not installable', problem: true })
    expect(sharedState(sched({ installed: false }), v)).toMatchObject({ state: 'not installed', problem: true })
    expect(sharedState(sched({ runs: [{ run_id: 'r', status: 'failed', exit_code: 2, start_ts: 'x' }] }), v)).toMatchObject({ state: 'last run failed', problem: true })
    expect(sharedState(sched({ installed: null }), v)).toMatchObject({ state: 'unknown', problem: false })
    expect(sharedState(sched({ line: 'legacy' }), v)).toMatchObject({ state: 'legacy line', problem: false })
    expect(sharedState(sched(), v)).toMatchObject({ state: 'ok', problem: false })
  })

  it('daemon: a stopped or stale scheduler outranks everything but disabled', () => {
    const s = sched({ line: 'daemon', installed: null, runs: [{ run_id: 'r', status: 'failed', exit_code: 1, start_ts: 'x' }] })
    expect(sharedState(s, daemonVenv([], null))).toMatchObject({ state: 'scheduler stopped', problem: true })
    expect(sharedState(s, daemonVenv([], 'stopped'))).toMatchObject({ state: 'scheduler stopped', problem: true })
    expect(sharedState(s, daemonVenv([], 'stale')).state).toBe('scheduler stale')
    expect(sharedState(s, daemonVenv([], 'running')).state).toBe('last run failed')
    // `every` is fine on the daemon, and its installed flag isn't a crontab
    expect(sharedState(sched({ line: 'daemon', when: { every: '5m' }, installable: false }), daemonVenv([], 'running')).state).toBe('ok')
  })
})

describe('collectRows', () => {
  it('makes one row per schedule, stale line and unreadable venv', () => {
    const venvs: SharedSchedulesVenv[] = [
      { ...cronVenv([sched()]), status: { ok: true, venv: 'ops', config: 'c', schedules: [sched()], stale: [{ id: 'gone', user: 'svc' }], errors: ['bad toml'] } },
      daemonVenv([sched({ id: 'poll', line: 'daemon', when: { every: '5m' } })], 'running', { config_error: 'boom' }),
      { host: 'db-1', group: 'legacy', available: true, ok: false, error: 'sudo: a password is required', owner: 'root' },
      { host: 'db-1', group: 'web', available: false, ok: true },
    ]
    const c = collectRows([consoleSched()], venvs, { hostGroups: { 'web-1': 'prod' } })
    const byKey = Object.fromEntries(c.rows.map(r => [r.key, r]))
    expect(byKey['console::rs-1']).toMatchObject({ source: 'console', host: 'local', runnable: 'disk-usage', argsText: '--path=/', state: 'ok' })
    expect(byKey['web-1::ops::nightly']).toMatchObject({ source: 'cron', hostGroup: 'prod', owner: 'svc', runAs: 'svc', historySearch: 'sched:nightly' })
    expect(byKey['web-1::ops::stale::svc::gone']).toMatchObject({ kind: 'stale', state: 'stale line', problem: true })
    expect(byKey['job-1::jobs::poll']).toMatchObject({ source: 'daemon', when: 'every 5m' })
    expect(byKey['db-1::legacy::unreadable']).toMatchObject({ kind: 'unreadable', state: "can't read", source: 'unknown', problem: true })
    expect(c.without).toEqual(['db-1/web'])
    expect(c.daemons.map(d => d.key)).toEqual(['job-1::jobs'])
    expect(c.notes.map(n => n.message)).toEqual(['bad toml', "The scheduler can't load its config: boom"])
  })

  it('takes a console schedule’s last run from its newest run card', () => {
    const card = (id: string, column: string, created_at: number): RunCard => ({
      card_id: id, trigger: 't', source: 'schedule', event_type: 'runnable', summary: '', column, created_at,
      detail: { schedule_id: 'rs-1', exit_code: column === 'failed' ? 3 : 0 },
    })
    const c = collectRows([consoleSched()], [], { cards: [card('a', 'done', 100), card('b', 'failed', 200)] })
    expect(c.rows[0]).toMatchObject({ state: 'last run failed', problem: true, lastRun: { status: 'failed', exitCode: 3 } })
  })

  it('a disabled console schedule has no next fire', () => {
    const c = collectRows([consoleSched({ enabled: false })], [])
    expect(c.rows[0]).toMatchObject({ state: 'disabled', nextRun: null })
  })
})

describe('scopeCounts + inScope', () => {
  it('counts schedules and problems per card; an unreadable venv shows on both host cards', () => {
    const c = collectRows(
      [consoleSched()],
      [
        cronVenv([sched(), sched({ id: 'b', installed: false })]),
        daemonVenv([sched({ id: 'p', line: 'daemon' })], 'stopped'),
        { host: 'x', group: 'y', available: true, ok: false, error: 'e' },
      ],
    )
    const n = scopeCounts(c)
    expect(n.console).toEqual({ schedules: 1, problems: 0 })
    expect(n.cron).toEqual({ schedules: 2, problems: 2 })    // not installed + can't read
    expect(n.daemon).toEqual({ schedules: 1, problems: 2 })  // stopped + can't read
    expect(n.all).toEqual({ schedules: 4, problems: 3 })
    expect(inScope('unknown', 'console')).toBe(false)
  })
})

describe('filterRows', () => {
  const c = collectRows(
    [consoleSched({ nextRun: '2026-11-07T07:00:00Z' })],
    [
      cronVenv([sched({ next_run: '2026-11-07T09:00:00Z' }), sched({ id: 'report', runnable: 'report', installed: false, next_run: null })], { host: 'web-1' }),
      { ...cronVenv([sched({ id: 'other', next_run: '2026-11-07T06:30:00Z', run_as: 'app' })]), host: 'db-1', owner: 'dba' },
    ],
    { hostGroups: { 'web-1': 'prod', 'db-1': 'data' } },
  )

  it('sorts by next fire by default, nothing-scheduled last', () => {
    expect(filterRows(c.rows, 'all', DEFAULT_FILTERS).map(r => r.id)).toEqual(['other', 'rs-1', 'nightly', 'report'])
  })

  it('applies the card, host:/group: tokens and free text', () => {
    expect(filterRows(c.rows, 'console', DEFAULT_FILTERS).map(r => r.id)).toEqual(['rs-1'])
    expect(filterRows(c.rows, 'all', { ...DEFAULT_FILTERS, search: 'host:web' }).map(r => r.id)).toEqual(['nightly', 'report'])
    expect(filterRows(c.rows, 'all', { ...DEFAULT_FILTERS, search: 'group:data' }).map(r => r.id)).toEqual(['other'])
    expect(filterRows(c.rows, 'all', { ...DEFAULT_FILTERS, search: 'report' }).map(r => r.id)).toEqual(['report'])
    expect(filterRows(c.rows, 'all', { ...DEFAULT_FILTERS, search: 'target=prod host:db' }).map(r => r.id)).toEqual(['other'])
  })

  it('applies the quick filters', () => {
    expect(filterRows(c.rows, 'all', { ...DEFAULT_FILTERS, onlyProblems: true }).map(r => r.id)).toEqual(['report'])
    expect(filterRows(c.rows, 'all', { ...DEFAULT_FILTERS, source: ['console'] }).map(r => r.id)).toEqual(['rs-1'])
    expect(filterRows(c.rows, 'all', { ...DEFAULT_FILTERS, owners: ['dba'] }).map(r => r.id)).toEqual(['other'])
    expect(filterRows(c.rows, 'all', { ...DEFAULT_FILTERS, runAs: ['app'] }).map(r => r.id)).toEqual(['other'])
    expect(filterRows(c.rows, 'all', { ...DEFAULT_FILTERS, states: ['not installed'] }).map(r => r.id)).toEqual(['report'])
  })

  it('sorts by host and runnable', () => {
    expect(filterRows(c.rows, 'cron', { ...DEFAULT_FILTERS, sort: 'host' }).map(r => r.id)).toEqual(['other', 'nightly', 'report'])
    expect(filterRows(c.rows, 'all', { ...DEFAULT_FILTERS, sort: 'runnable' }).map(r => r.runnable)).toEqual(['backup', 'backup', 'disk-usage', 'report'])
  })
})

describe('labels', () => {
  it('timeoutLabel', () => {
    expect(timeoutLabel(0)).toBe('no limit')
    expect(timeoutLabel(null)).toBe('6h (default)')
    expect(timeoutLabel(undefined)).toBe('6h (default)')
    expect(timeoutLabel(600)).toBe('10m')
  })
  it('shortDuration', () => {
    expect(shortDuration(45)).toBe('45s')
    expect(shortDuration(7260)).toBe('2h 1m')
    expect(shortDuration(3 * 86400 + 3600)).toBe('3d 1h')
  })
})

/**
 * allSchedules: rows from console schedules + each venv's runspec-scheduler
 * status (cron or daemon backend), one state per row, card counts, filters, sort.
 */
import { describe, it, expect } from 'vitest'
import {
  CLAIM_STALE_HOURS, DEFAULT_FILTERS, collectRows, daemonNeedsAttention, daemonSummary, filterRows, inScope, noteBatches, noteText, noteVerbs, noteable, scopeCounts, settingText, settingsChanges, settingsForm, sharedState, sharedWhenText, venvSettingsChanges, timeoutLabel, shortDuration,
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

describe('daemon settings', () => {
  it('writes seconds as the shortest exact duration', () => {
    expect(settingText(21600)).toBe('6h')
    expect(settingText(5400)).toBe('90m')
    expect(settingText(86400)).toBe('1d')
    expect(settingText(45)).toBe('45s')
    expect(settingText(0)).toBe('0')
    expect(settingText(null)).toBe('')
  })

  it('sends only what changed', () => {
    const cur = { backend: 'daemon', timeout: 21600, misfire_grace: 300, catch_up_window: 86400 }
    expect(settingsChanges(cur, { timeout: '6h', misfire_grace: '5m', catch_up_window: '1d' })).toBeNull()
    expect(settingsChanges(cur, { timeout: ' 2H ', misfire_grace: '5m', catch_up_window: '' })).toEqual({ timeout: '2h' })
    // an older scheduler reports no settings: whatever is typed is sent
    expect(settingsChanges(null, { misfire_grace: '10m' })).toEqual({ misfire_grace: '10m' })
  })

  it('carries settings, keepers and the log path onto the daemon strip', () => {
    const v = daemonVenv([], 'running', { version: '0.6.2', restart_needed: true })
    v.status = {
      ...v.status!,
      settings: { backend: 'daemon', timeout: 0, misfire_grace: 300, catch_up_window: 86400 },
      log: '/v/logs/scheduler.log',
      keeper: [{ manager: 'supervisor', where: 'a' }, { manager: 'cron', where: 'crontab' }, { manager: 'cron', where: 'crontab' }],
    }
    const [d] = collectRows([], [v]).daemons
    expect(d).toMatchObject({ keepers: ['supervisor', 'cron'], log: '/v/logs/scheduler.log', settings: { timeout: 0 } })
    expect(d.daemon?.restart_needed).toBe(true)
    // an older scheduler: no settings, no keepers
    expect(collectRows([], [daemonVenv([], 'stopped')]).daemons[0]).toMatchObject({ settings: null, keepers: [], log: '' })
  })
})

describe('daemon summary', () => {
  it('counts states and restarts, and flags what needs a look', () => {
    const ds = collectRows([], [
      daemonVenv([], 'running'),
      { ...daemonVenv([], 'running', { restart_needed: true }), host: 'job-2' },
      { ...daemonVenv([], 'stopped'), host: 'job-3' },
    ]).daemons
    expect(daemonSummary(ds)).toBe('3 scheduler daemons: 2 running, 1 stopped, 1 needs a restart')
    expect(ds.map(daemonNeedsAttention)).toEqual([false, true, true])
    expect(daemonSummary(ds.slice(0, 1))).toBe('1 scheduler daemon: 1 running')
  })
})

describe('who is handling a failure', () => {
  const NOW = Date.parse('2026-11-07T12:00:00Z')
  const failedRun = { run_id: 'r2', status: 'failed', exit_code: 1, start_ts: '2026-11-07T02:00:00Z' }
  const att = (state: 'failed' | 'handling' | 'done', ts = '2026-11-07T11:00:00Z') => ({
    state, failures: 2, since: '2026-11-06T02:00:00Z', run_id: 'r2',
    note: state === 'failed' ? null : { state, actor: 'ana', note: 'quota raised', ts },
  })
  const v = cronVenv([])

  it('a claim or a dealt-with note replaces "last run failed"', () => {
    expect(sharedState(sched({ runs: [failedRun], attention: att('failed') }), v, NOW)).toMatchObject({ state: 'last run failed', problem: true })
    const h = sharedState(sched({ runs: [failedRun], attention: att('handling') }), v, NOW)
    expect(h).toMatchObject({ state: 'being handled', problem: false })
    expect(h.tip).toContain('ana, 1h ago: quota raised')
    expect(sharedState(sched({ runs: [failedRun], attention: att('done') }), v, NOW)).toMatchObject({ state: 'dealt with', problem: false })
  })

  it('a stale claim counts as a problem again', () => {
    const old = new Date(NOW - (CLAIM_STALE_HOURS + 1) * 3600_000).toISOString()
    expect(sharedState(sched({ runs: [failedRun], attention: att('handling', old) }), v, NOW)).toMatchObject({ state: 'being handled', problem: true })
  })

  it('a stopped scheduler still outranks a claim', () => {
    const s = sched({ line: 'daemon', runs: [failedRun], attention: att('handling') })
    expect(sharedState(s, daemonVenv([], 'stopped'), NOW).state).toBe('scheduler stopped')
  })

  it('only failing host schedules are selectable, batched per venv', () => {
    const a = sched({ id: 'a', runs: [failedRun], attention: att('failed') })
    const b = sched({ id: 'b', runs: [{ ...failedRun, run_id: 'r9' }], attention: { ...att('handling'), run_id: 'r9' } })
    const c = sched({ id: 'c', attention: { state: 'clean', failures: 0, since: null, run_id: null, note: null } })
    const other = cronVenv([sched({ id: 'd', runs: [failedRun], attention: att('failed') })], { host: 'web-2' })
    const { rows } = collectRows([consoleSched()], [cronVenv([a, b, c]), other])
    expect(rows.filter(noteable).map(r => r.id)).toEqual(['a', 'b', 'd'])
    expect(noteBatches(rows)).toEqual([
      { host: 'web-1', venv: 'ops', runs: ['r2', 'r9'], ids: ['a', 'b'] },
      { host: 'web-2', venv: 'ops', runs: ['r2'], ids: ['d'] },
    ])
    expect(noteVerbs(rows.filter(r => r.id === 'a'))).toEqual(['claim', 'resolve'])
    expect(noteVerbs(rows.filter(r => r.id === 'a' || r.id === 'b'))).toEqual(['claim', 'resolve', 'release'])
    expect(noteVerbs(rows.filter(r => r.id === 'c'))).toEqual([])
  })

  it('noteText names who, how long ago and what', () => {
    expect(noteText({ state: 'done', actor: 'ana', note: 'fixed', ts: '2026-11-07T11:30:00Z' }, NOW)).toBe('ana, 30m ago: fixed')
    expect(noteText({ state: 'handling', os_user: 'svc', ts: '2026-11-05T12:00:00Z' }, NOW)).toBe('svc, 2d ago')
  })
})

describe('package schedules (runspec-scheduler 0.10.0)', () => {
  const venv = (backend: 'cron' | 'daemon'): SharedSchedulesVenv => ({
    group: 'ops', available: true, ok: true,
    status: { ok: true, venv: 'ops', config: 'c', backend, schedules: [], stale: [], errors: [] },
  })
  const pkg = (over: Partial<SharedSchedule> = {}): SharedSchedule => ({
    id: 'x.nightly', runnable: 'x', enabled: false, offered: true, origin: 'package',
    when: { cron: '51 2 * * *', timezone: 'local' }, package: { words: '02:30 daily', overridden: [] },
    installable: true, not_installable_reason: null, installed: false, line: null, next_run: null, runs: [], last_output: null,
    ...over,
  })
  const NEEDS = 'timezone America/New_York: this host is on Europe/London, and a crontab line fires on the host’s clock — this schedule needs the daemon backend (runspec-scheduler use daemon), or leave its timezone blank'

  it('shows an offered schedule as offered, not a problem', () => {
    const st = sharedState(pkg(), venv('cron'))
    expect(st.state).toBe('offered')
    expect(st.problem).toBe(false)
    expect(st.tip).toContain('02:30 daily')
    expect(sharedState(pkg({ installable: false, not_installable_reason: NEEDS }), venv('cron')).tip).toContain('need the daemon')
  })

  it('says "needs daemon" for a turned-on schedule cron keeps the wrong clock for', () => {
    const st = sharedState(pkg({ offered: false, enabled: true, installable: false, not_installable_reason: NEEDS }), venv('cron'))
    expect(st).toMatchObject({ state: 'needs daemon', problem: true })
    expect(sharedState(pkg({ offered: false, enabled: true, installable: false, not_installable_reason: 'no such runnable' }), venv('cron')).state).toBe('not installable')
  })

  it('times a package schedule in its words unless this venv changed the time', () => {
    expect(sharedWhenText(pkg())).toBe('02:30 daily')
    expect(sharedWhenText(pkg({ when: { cron: '0 9 * * *' }, package: { words: '02:30 daily', overridden: ['when'] } }))).toBe('0 9 * * *')
    expect(sharedWhenText({ ...pkg(), origin: undefined, package: undefined })).toBe('51 2 * * *')  // "local" isn't shown
  })

  it('collects the venv settings and package errors', () => {
    const v = venv('cron')
    v.status!.settings = { backend: 'cron', timeout: 21600, misfire_grace: 300, catch_up_window: 86400, timezone: 'Europe/Paris', package_schedules: 'auto' }
    v.status!.host_zone = 'Europe/London'
    v.status!.packages = { policy: 'auto', offered: 0, errors: ['runspec-acme: bad [x.meta.scheduler.y]: no time'], orphans: [] }
    v.status!.schedules = [pkg({ offered: false, enabled: true, description: 'Tidy' })]
    const c = collectRows([], [v])
    expect(c.venvs[0]).toMatchObject({ hostZone: 'Europe/London', packages: { policy: 'auto' } })
    expect(c.notes.map(n => n.message)).toEqual(['runspec-acme: bad [x.meta.scheduler.y]: no time'])
    expect(c.rows[0]).toMatchObject({ fromPackage: true, description: 'Tidy', when: '02:30 daily' })
  })

  it('saves only the changed zone / policy / durations', () => {
    const settings = { backend: 'daemon', timeout: 21600, misfire_grace: 300, catch_up_window: 86400, timezone: '', package_schedules: 'offer' as const }
    const v = { backend: 'daemon' as const, settings, packages: { policy: 'offer' as const, offered: 0, errors: [], orphans: [] } }
    const form = settingsForm(v)
    expect(form).toEqual({ timeout: '6h', misfire_grace: '5m', catch_up_window: '1d', timezone: '', package_schedules: 'offer' })
    expect(venvSettingsChanges(v, form)).toBeNull()
    expect(venvSettingsChanges(v, { ...form, timezone: ' Asia/Tokyo ', package_schedules: 'off' })).toEqual({ timezone: 'Asia/Tokyo', package_schedules: 'off' })
    // clearing a zone goes back to the host's: '' is sent
    const tokyo = { ...v, settings: { ...settings, timezone: 'Asia/Tokyo' } }
    expect(venvSettingsChanges(tokyo, { ...settingsForm(tokyo), timezone: '' })).toEqual({ timezone: '' })
    // an older scheduler: durations only (daemon), nothing on cron
    const old = { backend: 'cron' as const, settings: null, packages: null }
    expect(settingsForm(old)).toEqual({})
    expect(venvSettingsChanges(old, { timezone: 'Asia/Tokyo' })).toBeNull()
  })
})

/**
 * allSchedules.ts — the pure half of the Schedules tab's *All schedules* list:
 * turn this console's own schedules plus every venv's `runspec-scheduler status`
 * (across the active profile's connected hosts) into one flat row list, give each
 * row a single state tag, count what the four option cards show, and filter /
 * sort the list. No React, no bridge — unit-tested in allSchedules.test.ts.
 *
 * Older schedulers (< 0.5.0) report no `backend` and no `daemon`: they are cron.
 */
import type { PackageSchedulePolicy, RunCard, RunNote, Schedule, ScheduleAttention, SchedulerSettings, SharedSchedule, SharedSchedulesPackages, SharedSchedulesVenv } from '../bridge'
import { hostPasses, parseSearch } from '../searchTokens'

/** The four option cards across the top of the Schedules tab. */
export type ScheduleScope = 'all' | 'console' | 'cron' | 'daemon'
/** Where a row's schedule fires. `unknown` = a host venv whose status couldn't be read. */
export type RowSource = 'console' | 'cron' | 'daemon' | 'unknown'
export type RowKind = 'schedule' | 'stale' | 'unreadable'

export type RowState =
  | 'ok' | 'disabled' | 'last run failed' | 'running' | 'not installed' | 'not installable'
  | 'stale line' | 'scheduler stopped' | 'scheduler stale' | "can't read" | 'unknown' | 'legacy line'
  | 'being handled' | 'dealt with' | 'offered' | 'needs daemon'

export interface RowLastRun {
  ts: string
  status: string            // completed / failed / crashed / skipped / running
  exitCode: number | null
  durationMs?: number | null
}

export interface ScheduleRow {
  key: string
  kind: RowKind
  source: RowSource
  host: string
  hostGroup: string          // the host's sidebar group (for group: tokens)
  venv: string               // '' for the console's own schedules
  id: string
  name: string               // display name (console schedules may set one)
  runnable: string           // runnable + subcommand, or the console action
  argsText: string           // short `--k=v` summary
  when: string
  nextRun: string | null
  lastRun: RowLastRun | null
  state: RowState
  stateTip: string
  problem: boolean
  enabled: boolean
  owner: string
  runAs: string
  historySearch: string      // what the History tab's search box should hold
  shared?: SharedSchedule
  console?: Schedule
  error?: string
  /** A failing host schedule (claimable): its job state from the venv's runs index. */
  attention?: ScheduleAttention
  /** A host schedule a package offers in its runspec.toml (runspec-scheduler >= 0.10.0). */
  fromPackage?: boolean
  /** What the schedule does, in its package's words. */
  description?: string
}

/** A venv-level note shown above the list (status errors, daemon config errors). */
export interface VenvNote {
  key: string
  host: string
  venv: string
  source: RowSource
  message: string
}

/** A daemon-backed venv's scheduler process, for the strip on the Host daemon card. */
export interface DaemonVenv {
  key: string
  host: string
  venv: string
  owner: string
  daemon: NonNullable<NonNullable<SharedSchedulesVenv['status']>['daemon']> | null
  /** The `[scheduler]` durations (runspec-scheduler >= 0.7.0; null before). */
  settings: SchedulerSettings | null
  /** What keeps the daemon running (systemd-user, supervisor, cron, systemd). */
  keepers: string[]
  log: string
}

/** A venv whose runspec-scheduler status was read — a place a host schedule can
 *  be added (the editor's "Where" choices). */
export interface SchedulerVenv {
  key: string
  host: string
  hostGroup: string
  venv: string
  backend: 'cron' | 'daemon'
  owner: string
  /** The `[scheduler]` table (null on schedulers < 0.7.0). */
  settings: SchedulerSettings | null
  /** The host's own zone (runspec-scheduler >= 0.10.0; '' before). */
  hostZone: string
  /** Package schedules (runspec-scheduler >= 0.10.0; null before). */
  packages: SharedSchedulesPackages | null
}

export interface Collected {
  rows: ScheduleRow[]
  /** Every venv whose scheduler status was read, in host/venv order. */
  venvs: SchedulerVenv[]
  notes: VenvNote[]
  daemons: DaemonVenv[]
  /** `host/venv` of each venv with no runspec-scheduler installed. */
  without: string[]
}

export const STATE_COLOR: Record<RowState, string> = {
  'ok': 'green',
  'running': 'blue',
  'disabled': 'default',
  'last run failed': 'red',
  'not installed': 'orange',
  'not installable': 'orange',
  'stale line': 'orange',
  'scheduler stopped': 'red',
  'scheduler stale': 'orange',
  "can't read": 'red',
  'unknown': 'default',
  'legacy line': 'gold',
  'being handled': 'purple',
  'dealt with': 'cyan',
  'offered': 'geekblue',
  'needs daemon': 'orange',
}

/** A claim with no newer note for this long shows as stale: the failure counts
 *  as a problem again, so a forgotten claim doesn't hide it. */
export const CLAIM_STALE_HOURS = 4

const ageText = (iso: string, now: number): string => {
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return ''
  const min = Math.max(0, Math.round((now - t) / 60000))
  if (min < 60) return `${min}m`
  const h = Math.round(min / 60)
  return h < 48 ? `${h}h` : `${Math.round(h / 24)}d`
}

/** One line for a note: "jason, 2h ago: disk full". */
export function noteText(n: RunNote | null | undefined, now = Date.now()): string {
  if (!n) return ''
  const who = n.actor || n.os_user || 'someone'
  const age = n.ts ? ageText(n.ts, now) : ''
  return `${who}${age ? `, ${age} ago` : ''}${n.note ? `: ${n.note}` : ''}`
}

/** Whether a claim has gone stale (no newer note for CLAIM_STALE_HOURS). */
export function claimStale(n: RunNote | null | undefined, now = Date.now()): boolean {
  if (!n || n.state !== 'handling') return false
  const t = Date.parse(n.ts)
  return !Number.isNaN(t) && now - t > CLAIM_STALE_HOURS * 3600_000
}

export function venvBackend(v: SharedSchedulesVenv): 'cron' | 'daemon' {
  return v.status?.backend === 'daemon' ? 'daemon' : 'cron'
}

function whenText(w: { cron?: string; every?: string; timezone?: string } | undefined): string {
  const base = w?.cron ? w.cron : w?.every ? `every ${w.every}` : '—'
  // "local" = the venv's zone, which is how every other schedule fires too.
  return w?.timezone && w.timezone.toLowerCase() !== 'local' ? `${base} (${w.timezone})` : base
}

/** A host schedule's timing for a person: a package schedule in its package's
 *  words (unless this venv changed its time). */
export function sharedWhenText(s: SharedSchedule): string {
  const pkg = s.package
  if (pkg?.words && !(pkg.overridden ?? []).includes('when')) return pkg.words
  return whenText(s.when)
}

/** Whether cron can't take a schedule because it keeps another zone's time. */
export function needsDaemon(s: SharedSchedule): boolean {
  return !s.installable && /needs the daemon/i.test(s.not_installable_reason ?? '')
}

/** A package schedule this venv changed (time, args, …): Put back undoes that. */
export function packageOverridden(s: SharedSchedule): boolean {
  return s.origin === 'package' && (s.package?.overridden ?? []).length > 0
}

function argsSummary(args: Record<string, unknown> | undefined, dashes = true): string {
  return Object.entries(args ?? {}).map(([k, v]) => `${dashes ? '--' : ''}${k}=${String(v)}`).join(' ')
}

const FAILED = new Set(['failed', 'crashed'])

/** One state tag for a host schedule, most important first. */
export function sharedState(s: SharedSchedule, v: SharedSchedulesVenv, now = Date.now()): { state: RowState; tip: string; problem: boolean } {
  const backend = venvBackend(v)
  if (s.offered) {
    const words = s.package?.words ? ` (${s.package.words})` : ''
    const zone = backend === 'cron' && needsDaemon(s)
      ? ' It keeps another zone’s time, so on this cron venv it would need the daemon.'
      : ''
    return { state: 'offered', tip: `Offered by its package${words}. Off until someone turns it on.${zone}`, problem: false }
  }
  if (!s.enabled) {
    const tip = s.origin === 'package' ? 'Turned off in this venv — never fires.' : 'Disabled in runspec_scheduler.toml — never fires.'
    return { state: 'disabled', tip, problem: false }
  }
  if (backend === 'daemon') {
    const d = v.status?.daemon
    if (!d || d.state === 'stopped') {
      return { state: 'scheduler stopped', tip: 'This venv is set to the daemon backend but its runspec-scheduler isn’t running, so nothing fires. Start it (or its keeper) on the host.', problem: true }
    }
    if (d.state === 'stale') {
      const age = d.heartbeat_age_s != null ? ` (last heartbeat ${Math.round(d.heartbeat_age_s)}s ago)` : ''
      return { state: 'scheduler stale', tip: `The scheduler process stopped reporting${age} — it may be hung.`, problem: true }
    }
  } else {
    if (needsDaemon(s)) {
      return { state: 'needs daemon', tip: `${s.not_installable_reason} Move the venv to the daemon (Update venv…), or edit the schedule’s zone.`, problem: true }
    }
    if (!s.installable) return { state: 'not installable', tip: s.not_installable_reason || 'Cron can’t run this schedule.', problem: true }
    if (s.installed === false) return { state: 'not installed', tip: 'In the config but not in the crontab — run `runspec-scheduler sync` on the host.', problem: true }
  }
  const last = s.runs[0]
  const att = s.attention
  if (att && att.state !== 'clean' && att.state !== 'failed' && att.note) {
    // Someone has said they are on it, or that it was dealt with; it covers the
    // schedule's failures until its next success.
    const fails = `${att.failures} failed run${att.failures === 1 ? '' : 's'} since the last success`
    if (att.state === 'done') {
      return { state: 'dealt with', tip: `Dealt with by ${noteText(att.note, now)}. Covers ${fails}; the next success clears it.`, problem: false }
    }
    const stale = claimStale(att.note, now)
    return {
      state: 'being handled',
      tip: `${stale ? `Claimed over ${CLAIM_STALE_HOURS}h ago with no update, so it counts as a problem again. ` : ''}Being handled by ${noteText(att.note, now)}. Covers ${fails}.`,
      problem: stale,
    }
  }
  if (last && FAILED.has(last.status)) {
    const why = last.exit_code != null ? `exit ${last.exit_code}` : last.status
    return { state: 'last run failed', tip: `The last fire ended ${why}. Expand the row for its output, or tick it to say you're on it.`, problem: true }
  }
  if (last && last.status === 'running') return { state: 'running', tip: 'Firing now.', problem: false }
  if (backend === 'cron' && s.installed === null) {
    return { state: 'unknown', tip: `Couldn't read ${s.run_as || 'the owner'}'s crontab without a password prompt.`, problem: false }
  }
  if (s.line === 'legacy') {
    return { state: 'legacy line', tip: 'Installed by an older runspec-scheduler: fires run unrecorded. Re-run `runspec-scheduler sync` on the host.', problem: false }
  }
  return { state: 'ok', tip: backend === 'daemon' ? 'Fired by this venv’s runspec-scheduler daemon.' : 'Installed in the crontab; every fire is recorded.', problem: false }
}

/** The console action a schedule runs, as one short label. */
export function consoleActionLabel(s: Schedule): string {
  switch (s.action) {
    case 'event': return `event ${s.event ?? ''}`.trim()
    case 'prompt': { const p = (s.prompt ?? '').replace(/\s+/g, ' ').trim(); return `prompt: ${p.length > 48 ? `${p.slice(0, 48)}…` : p}` }
    case 'runbook': return `runbook ${s.runbook ?? ''}`.trim()
    case 'saved_run': return `saved run ${s.saved_run ?? ''}`.trim()
    case 'saved_transfer': return `saved transfer ${s.saved_transfer ?? ''}`.trim()
    case 'eval': return `evals ${s.eval_fixture ?? ''}`.trim()
    default: return [s.runnable ?? '', ...(s.command ?? [])].join(' ').trim()
  }
}

/** The newest schedule run card for a console schedule (runnable action only). */
function lastConsoleRun(id: string, cards: RunCard[]): RowLastRun | null {
  let best: RunCard | null = null
  for (const c of cards) {
    const sid = (c.detail as Record<string, unknown> | undefined)?.schedule_id
    if (sid !== id) continue
    if (!best || (c.created_at ?? 0) > (best.created_at ?? 0)) best = c
  }
  if (!best) return null
  const detail = (best.detail ?? {}) as Record<string, unknown>
  const status = best.column === 'done' ? 'completed' : best.column === 'failed' ? 'failed' : best.column === 'running' ? 'running' : String(best.column)
  const ec = detail.exit_code
  return { ts: new Date((best.created_at ?? 0) * 1000).toISOString(), status, exitCode: typeof ec === 'number' ? ec : null }
}

export function collectRows(
  consoleSchedules: Schedule[],
  venvs: SharedSchedulesVenv[],
  opts: { cards?: RunCard[]; hostGroups?: Record<string, string> } = {},
): Collected {
  const groups = opts.hostGroups ?? {}
  const rows: ScheduleRow[] = []
  const notes: VenvNote[] = []
  const daemons: DaemonVenv[] = []
  const without: string[] = []
  const editable: SchedulerVenv[] = []

  for (const s of consoleSchedules) {
    const enabled = s.enabled !== false
    const lastRun = lastConsoleRun(s.id, opts.cards ?? [])
    const failed = enabled && !!lastRun && FAILED.has(lastRun.status)
    const state: RowState = !enabled ? 'disabled' : failed ? 'last run failed' : lastRun?.status === 'running' ? 'running' : 'ok'
    const detail = s.action === 'event' ? s.fields : s.action === 'runnable' ? s.args : s.values
    rows.push({
      key: `console::${s.id}`,
      kind: 'schedule',
      source: 'console',
      host: 'local',
      hostGroup: groups.local ?? '',
      venv: '',
      id: s.id,
      name: s.name ?? '',
      runnable: consoleActionLabel(s),
      argsText: argsSummary(detail as Record<string, unknown> | undefined, s.action === 'runnable'),
      when: whenText(s.when),
      nextRun: enabled ? (s.nextRun ?? null) : null,
      lastRun,
      state,
      stateTip: !enabled ? 'Disabled — won’t fire.' : failed ? 'The last run from this console failed.' : 'Fires from this console while it is open.',
      problem: failed,
      enabled,
      owner: '',
      runAs: '',
      historySearch: s.action === 'runnable' ? (s.runnable ?? '') : '',
      console: s,
    })
  }

  for (const v of venvs) {
    const host = v.host ?? 'local'
    const venv = v.group
    const hostGroup = groups[host] ?? ''
    const base = `${host}::${venv}`
    if (!v.available) { without.push(`${host}/${venv}`); continue }
    if (!v.ok || !v.status) {
      rows.push({
        key: `${base}::unreadable`, kind: 'unreadable', source: 'unknown', host, hostGroup, venv,
        id: '', name: '', runnable: '', argsText: '', when: '—', nextRun: null, lastRun: null,
        state: "can't read", stateTip: v.error || 'runspec-scheduler status failed', problem: true, enabled: true,
        owner: v.owner ?? '', runAs: '', historySearch: '', error: v.error || 'runspec-scheduler status failed',
      })
      continue
    }
    const st = v.status
    const backend = venvBackend(v)
    editable.push({
      key: base, host, hostGroup, venv, backend, owner: v.owner ?? '',
      settings: st.settings ?? null, hostZone: st.host_zone ?? '', packages: st.packages ?? null,
    })
    for (const e of st.packages?.errors ?? []) notes.push({ key: `${base}::pkg::${e}`, host, venv, source: backend, message: e })
    for (const e of st.errors ?? []) notes.push({ key: `${base}::err::${e}`, host, venv, source: backend, message: e })
    if (backend === 'daemon') {
      daemons.push({
        key: base, host, venv, owner: v.owner ?? '', daemon: st.daemon ?? null,
        settings: st.settings ?? null,
        keepers: [...new Set((st.keeper ?? []).map(k => k.manager).filter(Boolean))],
        log: st.log ?? '',
      })
      if (st.daemon?.config_error) {
        notes.push({ key: `${base}::cfg`, host, venv, source: 'daemon', message: `The scheduler can't load its config: ${st.daemon.config_error}` })
      }
    }
    for (const s of st.schedules ?? []) {
      const { state, tip, problem } = sharedState(s, v)
      const r = s.runs[0]
      rows.push({
        key: `${base}::${s.id}`,
        kind: 'schedule',
        source: backend,
        host, hostGroup, venv,
        id: s.id,
        name: '',
        runnable: [s.runnable, ...(s.command ?? [])].join(' '),
        argsText: argsSummary(s.args),
        when: sharedWhenText(s),
        nextRun: s.enabled && s.installed !== false ? s.next_run : null,
        lastRun: r ? { ts: r.start_ts, status: r.status, exitCode: r.exit_code, durationMs: r.duration_ms } : null,
        state, stateTip: tip, problem,
        enabled: s.enabled,
        owner: v.owner ?? '',
        runAs: s.run_as ?? '',
        historySearch: `sched:${s.id}`,
        shared: s,
        ...(s.attention && s.attention.state !== 'clean' && s.attention.run_id ? { attention: s.attention } : {}),
        ...(s.origin === 'package' ? { fromPackage: true } : {}),
        ...(s.description ? { description: s.description } : {}),
      })
    }
    for (const x of st.stale ?? []) {
      const reason = x.reason ? ` (${x.reason})` : ''
      rows.push({
        key: `${base}::stale::${x.user}::${x.id}`, kind: 'stale', source: backend, host, hostGroup, venv,
        id: x.id, name: '', runnable: '', argsText: '', when: '—', nextRun: null, lastRun: null,
        state: 'stale line',
        stateTip: `A crontab line for ${x.user} that this venv's config no longer wants${reason} — it can still fire. \`runspec-scheduler sync\` on the host removes it.`,
        problem: true, enabled: true, owner: v.owner ?? '', runAs: x.user, historySearch: `sched:${x.id}`,
      })
    }
  }
  editable.sort((a, b) => a.host.localeCompare(b.host) || a.venv.localeCompare(b.venv))
  return { rows, venvs: editable, notes, daemons, without }
}

/** Does a row belong under a card? A can't-read venv shows on both host cards. */
export function inScope(source: RowSource, scope: ScheduleScope): boolean {
  if (scope === 'all') return true
  if (source === 'unknown') return scope === 'cron' || scope === 'daemon'
  return source === scope
}

export interface ScopeCount { schedules: number; problems: number }

/** Schedule and problem counts for each option card. */
export function scopeCounts(c: Collected): Record<ScheduleScope, ScopeCount> {
  const out = {} as Record<ScheduleScope, ScopeCount>
  for (const scope of ['all', 'console', 'cron', 'daemon'] as ScheduleScope[]) {
    const rows = c.rows.filter(r => inScope(r.source, scope))
    out[scope] = {
      schedules: rows.filter(r => r.kind === 'schedule').length,
      problems: rows.filter(r => r.problem).length + c.notes.filter(n => inScope(n.source, scope)).length,
    }
  }
  return out
}

export type SortKey = 'next' | 'last' | 'host' | 'runnable'

export interface RowFilters {
  search: string
  source: RowSource[]       // empty = any (only offered on the All card)
  states: RowState[]        // empty = any
  owners: string[]
  runAs: string[]
  onlyProblems: boolean
  sort: SortKey
}

export const DEFAULT_FILTERS: RowFilters = {
  search: '', source: [], states: [], owners: [], runAs: [], onlyProblems: false, sort: 'next',
}

const tsOf = (iso: string | null | undefined): number | null => {
  if (!iso) return null
  const t = Date.parse(iso)
  return Number.isNaN(t) ? null : t
}

/** Nulls last, whichever way the values run. */
function cmpNullable(a: number | null, b: number | null, dir: 1 | -1): number {
  if (a == null && b == null) return 0
  if (a == null) return 1
  if (b == null) return -1
  return (a - b) * dir
}

export function filterRows(rows: ScheduleRow[], scope: ScheduleScope, f: RowFilters): ScheduleRow[] {
  const p = parseSearch(f.search)
  const text = p.text.toLowerCase()
  const out = rows.filter(r => {
    if (!inScope(r.source, scope)) return false
    if (scope === 'all' && f.source.length && !f.source.includes(r.source)) return false
    if (f.states.length && !f.states.includes(r.state)) return false
    if (f.owners.length && !f.owners.includes(r.owner)) return false
    if (f.runAs.length && !f.runAs.includes(r.runAs)) return false
    if (f.onlyProblems && !r.problem) return false
    if (!hostPasses(p, r.host, r.hostGroup)) return false
    if (text) {
      const hay = `${r.id} ${r.name} ${r.runnable} ${r.argsText} ${r.venv}`.toLowerCase()
      if (!text.split(/\s+/).every(w => hay.includes(w))) return false
    }
    return true
  })
  const byName = (a: ScheduleRow, b: ScheduleRow) =>
    a.host.localeCompare(b.host) || a.venv.localeCompare(b.venv) || (a.name || a.id).localeCompare(b.name || b.id)
  out.sort((a, b) => {
    switch (f.sort) {
      case 'last': return cmpNullable(tsOf(a.lastRun?.ts), tsOf(b.lastRun?.ts), -1) || byName(a, b)
      case 'host': return byName(a, b)
      case 'runnable': return a.runnable.localeCompare(b.runnable) || byName(a, b)
      default: return cmpNullable(tsOf(a.nextRun), tsOf(b.nextRun), 1) || byName(a, b)
    }
  })
  return out
}

/** "3h 4m" from seconds — the daemon strip's uptime. */
export function shortDuration(sec: number | null | undefined): string {
  if (sec == null || Number.isNaN(sec)) return '—'
  const s = Math.max(0, Math.round(sec))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 48) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

/** Whether a venv's daemon wants a look: not running, or running old code. */
export function daemonNeedsAttention(d: DaemonVenv): boolean {
  const st = d.daemon?.state ?? 'stopped'
  return st !== 'running' || !!d.daemon?.restart_needed
}

/** The one-line summary over every daemon strip (shown when they're folded). */
export function daemonSummary(daemons: DaemonVenv[]): string {
  const n = (st: string) => daemons.filter(d => (d.daemon?.state ?? 'stopped') === st).length
  const restart = daemons.filter(d => d.daemon?.state === 'running' && d.daemon?.restart_needed).length
  const parts = [
    n('running') ? `${n('running')} running` : '',
    n('stale') ? `${n('stale')} stale` : '',
    n('stopped') ? `${n('stopped')} stopped` : '',
    restart ? `${restart} need${restart === 1 ? 's' : ''} a restart` : '',
  ].filter(Boolean)
  return `${daemons.length} scheduler daemon${daemons.length === 1 ? '' : 's'}: ${parts.join(', ')}`
}

/** How many attention-needing strips stay visible while the list is folded. */
export const FOLDED_ATTENTION_MAX = 3

/** A `[scheduler]` duration in seconds as the shortest exact text the
 *  scheduler accepts back (21600 → `6h`, 5400 → `90m`, 0 → `0`). */
export function settingText(sec: number | null | undefined): string {
  if (sec == null || Number.isNaN(sec)) return ''
  const s = Math.max(0, Math.round(sec))
  if (s === 0) return '0'
  if (s % 86400 === 0) return `${s / 86400}d`
  if (s % 3600 === 0) return `${s / 3600}h`
  if (s % 60 === 0) return `${s / 60}m`
  return `${s}s`
}

/** The daemon's three durations, keyed as `host_scheduler_settings` takes them. */
export const DAEMON_SETTINGS: { key: 'timeout' | 'misfire_grace' | 'catch_up_window'; label: string; help: string }[] = [
  { key: 'timeout', label: 'Fire timeout', help: 'How long one fire may run before it is stopped; 0 = no limit. A schedule can set its own.' },
  { key: 'misfire_grace', label: 'Misfire grace', help: 'How late a fire may still start (after a pause or a busy host); later than this it is recorded as skipped.' },
  { key: 'catch_up_window', label: 'Catch-up window', help: 'How far back a catch-up schedule looks for a missed fire when the daemon starts.' },
]

/** The changed durations only (blank / unchanged left out), or null if none. */
export function settingsChanges(
  current: SchedulerSettings | null,
  form: Partial<Record<'timeout' | 'misfire_grace' | 'catch_up_window', string>>,
): Partial<Record<'timeout' | 'misfire_grace' | 'catch_up_window', string>> | null {
  const out: Partial<Record<'timeout' | 'misfire_grace' | 'catch_up_window', string>> = {}
  for (const { key } of DAEMON_SETTINGS) {
    const v = (form[key] ?? '').trim().toLowerCase()
    if (!v) continue
    if (current && v === settingText(current[key])) continue
    out[key] = v
  }
  return Object.keys(out).length ? out : null
}

/** What the venv does with schedules its packages offer. */
export const PACKAGE_POLICY: { value: PackageSchedulePolicy; label: string; help: string }[] = [
  { value: 'offer', label: 'Offer', help: 'They show here, off, until someone turns each one on.' },
  { value: 'auto', label: 'Turn on', help: 'They run as soon as the package is installed (each can still be paused).' },
  { value: 'off', label: 'Ignore', help: 'They don’t show and never run.' },
]

/** The Settings dialog's fields for one venv. */
export interface VenvSettingsForm {
  timeout?: string
  misfire_grace?: string
  catch_up_window?: string
  timezone?: string                // '' = the host's zone
  package_schedules?: PackageSchedulePolicy
}

/** Whether the venv's scheduler takes a zone + package policy (0.10.0+). */
export function supportsPackages(v: Pick<SchedulerVenv, 'settings' | 'packages'>): boolean {
  return !!v.packages || v.settings?.package_schedules != null
}

/** The form a venv's Settings dialog opens with. */
export function settingsForm(v: Pick<SchedulerVenv, 'backend' | 'settings' | 'packages'>): VenvSettingsForm {
  const cur = v.settings
  const out: VenvSettingsForm = {}
  if (v.backend === 'daemon' && cur) for (const { key } of DAEMON_SETTINGS) out[key] = settingText(cur[key])
  if (supportsPackages(v)) {
    out.timezone = cur?.timezone ?? ''
    out.package_schedules = cur?.package_schedules ?? v.packages?.policy ?? 'offer'
  }
  return out
}

/** What a Settings save sends: the changed fields only (durations on a daemon
 *  venv; zone + policy on a 0.10.0+ scheduler), or null when nothing changed.
 *  A cleared zone is sent as '' (back to the host's). */
export function venvSettingsChanges(
  v: Pick<SchedulerVenv, 'backend' | 'settings' | 'packages'>,
  form: VenvSettingsForm,
): VenvSettingsForm | null {
  const out: VenvSettingsForm = v.backend === 'daemon' ? { ...(settingsChanges(v.settings, form) ?? {}) } : {}
  if (supportsPackages(v)) {
    const before = settingsForm(v)
    const zone = (form.timezone ?? '').trim()
    if (zone !== (before.timezone ?? '')) out.timezone = zone
    if (form.package_schedules && form.package_schedules !== before.package_schedules) out.package_schedules = form.package_schedules
  }
  return Object.keys(out).length ? out : null
}

/** A schedule's per-fire timeout as the daemon applies it. */
export function timeoutLabel(t: number | null | undefined): string {
  if (t === 0) return 'no limit'
  if (t == null) return '6h (default)'
  return shortDuration(t)
}

/** Whether a row can be claimed / marked dealt with / released: a host schedule
 *  that is failing (since its last success) on a runspec that keeps notes. */
export function noteable(r: ScheduleRow): boolean {
  return r.kind === 'schedule' && !!r.shared && !!r.attention?.run_id
}

export type NoteVerb = 'claim' | 'resolve' | 'release'

export interface NoteBatch { host: string; venv: string; runs: string[]; ids: string[] }

/** The selected rows as one call per venv: the newest failed run of each
 *  schedule (a note on it covers the schedule until its next success). */
export function noteBatches(rows: ScheduleRow[]): NoteBatch[] {
  const out = new Map<string, NoteBatch>()
  for (const r of rows) {
    if (!noteable(r)) continue
    const key = `${r.host}::${r.venv}`
    const b = out.get(key) ?? { host: r.host, venv: r.venv, runs: [], ids: [] }
    b.runs.push(r.attention!.run_id!)
    b.ids.push(r.id)
    out.set(key, b)
  }
  return [...out.values()]
}

/** Which verbs fit a selection: release only when something in it is claimed or dealt with. */
export function noteVerbs(rows: ScheduleRow[]): NoteVerb[] {
  const sel = rows.filter(noteable)
  if (!sel.length) return []
  const anyNoted = sel.some(r => r.attention?.state === 'handling' || r.attention?.state === 'done')
  return anyNoted ? ['claim', 'resolve', 'release'] : ['claim', 'resolve']
}

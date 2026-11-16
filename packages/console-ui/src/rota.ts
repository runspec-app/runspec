// Working hours (the rota) as the schedules see them: does a fire at a given
// time fall inside the operator's hours? Triggers and this console's schedules
// are skipped outside them, so the Schedules tab says so on each next run
// instead of promising a run that won't happen.

import type { Rota, RotaDay } from './bridge'

const DOW_KEY = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']
const DAY_ORDER = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
const DAY_SHORT: Record<string, string> = { mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun' }

const minutes = (hhmm: string | undefined, fallback: number): number => {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm ?? '')
  return m ? Number(m[1]) * 60 + Number(m[2]) : fallback
}

function day(rota: Rota, key: string): RotaDay | null {
  const d = rota[key]
  return d && typeof d === 'object' ? (d as RotaDay) : null
}

/** Whether the console's manual working hours are on. Outlook-driven hours
 *  can't be read ahead, so they count as unknown (never claimed). */
export function rotaGates(rota: Rota | null | undefined): boolean {
  return !!rota?.enabled && (rota.source ?? 'console') === 'console'
}

/** Does a fire at `when` fall inside working hours? `null` when the rota
 *  doesn't gate (off, or Outlook-driven). A shift that ends before it starts
 *  runs past midnight. Pure, local time. */
export function rotaAllows(rota: Rota | null | undefined, when: Date): boolean | null {
  if (!rota || !rotaGates(rota)) return null
  const t = when.getHours() * 60 + when.getMinutes()
  const today = day(rota, DOW_KEY[when.getDay()])
  const yesterday = day(rota, DOW_KEY[(when.getDay() + 6) % 7])
  const inShift = (d: RotaDay | null, part: 'same' | 'carry') => {
    if (!d?.on) return false
    const s = minutes(d.start, 0), e = minutes(d.end, 24 * 60)
    if (e > s) return part === 'same' && t >= s && t < e
    // Overnight: from start to midnight today, and from midnight to end tomorrow.
    return part === 'same' ? t >= s : t < e
  }
  return inShift(today, 'same') || inShift(yesterday, 'carry')
}

/** The hours in one short line: "Mon–Thu 09:00–17:30, Fri 09:00–16:00". Runs of
 *  days with the same hours are joined. */
export function rotaSummary(rota: Rota): string {
  const parts: string[] = []
  let run: { from: string; to: string; hours: string } | null = null
  for (const key of DAY_ORDER) {
    const d = day(rota, key)
    const hours = d?.on ? `${d.start ?? '00:00'}–${d.end ?? '24:00'}` : ''
    if (run && hours && run.hours === hours) { run.to = key; continue }
    if (run) parts.push(fmtRun(run))
    run = hours ? { from: key, to: key, hours } : null
  }
  if (run) parts.push(fmtRun(run))
  return parts.length ? parts.join(', ') : 'no days'
}

function fmtRun(r: { from: string; to: string; hours: string }): string {
  const days = r.from === r.to ? DAY_SHORT[r.from] : `${DAY_SHORT[r.from]}–${DAY_SHORT[r.to]}`
  return `${days} ${r.hours}`
}

// ── does a cron schedule ever fire inside working hours? ─────────────────────

const CRON_MACROS: Record<string, string> = {
  '@yearly': '0 0 1 1 *', '@annually': '0 0 1 1 *', '@monthly': '0 0 1 * *',
  '@weekly': '0 0 * * 0', '@daily': '0 0 * * *', '@midnight': '0 0 * * *', '@hourly': '0 * * * *',
}
const DOW_NAMES: Record<string, number> = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 }

/** The values a cron field allows within [lo, hi], or null when it can't be
 *  read. Handles `*`, `*\/n`, `a`, `a-b`, `a-b/n`, lists, and day names. */
function cronField(field: string, lo: number, hi: number, names: Record<string, number> = {}): Set<number> | null {
  const out = new Set<number>()
  const num = (t: string): number => {
    const n = names[t.toLowerCase()]
    return n !== undefined ? n : /^\d+$/.test(t) ? Number(t) : NaN
  }
  for (const part of field.split(',')) {
    const [range, stepText] = part.split('/')
    const step = stepText === undefined ? 1 : Number(stepText)
    if (!Number.isInteger(step) || step < 1) return null
    let a: number, b: number
    if (range === '*') { a = lo; b = hi }
    else if (range.includes('-')) { const [x, y] = range.split('-'); a = num(x); b = num(y) }
    else { a = num(range); b = stepText === undefined ? a : hi }
    if (Number.isNaN(a) || Number.isNaN(b)) return null
    for (let v = a; v <= b; v += step) out.add(v)
  }
  return out
}

/** Whether a cron schedule ever fires inside working hours: true / false, or
 *  null when the hours don't gate or the expression can't be read. Checks every
 *  minute of a week (day-of-month and month only pick dates, so they count as
 *  any day). A "false" schedule never runs while working hours are on. */
export function cronEverInRota(rota: Rota | null | undefined, cron: string): boolean | null {
  if (!rota || !rotaGates(rota)) return null
  const expr = CRON_MACROS[cron.trim().toLowerCase()] ?? cron.trim()
  const f = expr.split(/\s+/)
  if (f.length !== 5) return null
  const mins = cronField(f[0], 0, 59)
  const hours = cronField(f[1], 0, 23)
  const dows = f[4] === '*' ? new Set([0, 1, 2, 3, 4, 5, 6]) : cronField(f[4], 0, 7, DOW_NAMES)
  if (!mins || !hours || !dows) return null
  if (dows.has(7)) dows.add(0)
  // 2026-11-15 is a Sunday: day offset = day of week.
  for (const d of dows) {
    if (d > 6) continue
    for (const h of hours) for (const m of mins) {
      if (rotaAllows(rota, new Date(2026, 10, 15 + d, h, m))) return true
    }
  }
  return false
}

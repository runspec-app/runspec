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

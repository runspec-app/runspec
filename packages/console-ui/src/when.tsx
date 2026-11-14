// One way to show a moment, everywhere in the console: relative within a day
// ("just now", "5m ago", "in 3h"), otherwise "13 Nov 19:53" (this year) or
// "13 Nov 2024" (another year). The full local timestamp is on hover.
// Fixed English month names and 24h time so the same moment reads the same on
// every tab, whatever the machine's locale did with toLocaleString.

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const pad = (n: number) => String(n).padStart(2, '0')

function toDate(t: string | number | Date | null | undefined): Date | null {
  if (t == null || t === '') return null
  const d = t instanceof Date ? t : new Date(t)
  return Number.isNaN(d.getTime()) ? null : d
}

/** "5m ago" / "in 3h" / "13 Nov 19:53" / "13 Nov 2024"; "—" for no time.
 *  A number is milliseconds since the epoch. Pure, for tests. */
export function fmtWhen(t: string | number | Date | null | undefined, now = Date.now()): string {
  const d = toDate(t)
  if (!d) return '—'
  const diff = d.getTime() - now
  const a = Math.abs(diff)
  const rel = (v: string) => (diff < 0 ? `${v} ago` : `in ${v}`)
  if (a < 45_000) return 'just now'
  if (a < 3_600_000) return rel(`${Math.max(1, Math.round(a / 60_000))}m`)
  if (a < 86_400_000) return rel(`${Math.round(a / 3_600_000)}h`)
  const date = `${d.getDate()} ${MONTHS[d.getMonth()]}`
  return d.getFullYear() === new Date(now).getFullYear()
    ? `${date} ${pad(d.getHours())}:${pad(d.getMinutes())}`
    : `${date} ${d.getFullYear()}`
}

/** The full local timestamp, for a tooltip: "Fri 13 Nov 2026, 19:53:41". */
export function fullWhen(t: string | number | Date | null | undefined): string {
  const d = toDate(t)
  if (!d) return ''
  const day = d.toLocaleDateString(undefined, { weekday: 'short' })
  return `${day} ${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** A time of day for a log or a card header: "19:53:41" (24h, every locale),
 *  or "19:53" without seconds. "" for no time. Pure, for tests. */
export function fmtClock(t: string | number | Date | null | undefined, seconds = true): string {
  const d = toDate(t)
  if (!d) return ''
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return seconds ? `${hm}:${pad(d.getSeconds())}` : hm
}

/** A day: "13 Nov" this year, "13 Nov 2024" another. "" for no date. */
export function fmtDay(t: string | number | Date | null | undefined, now = Date.now()): string {
  const d = toDate(t)
  if (!d) return ''
  const day = `${d.getDate()} ${MONTHS[d.getMonth()]}`
  return d.getFullYear() === new Date(now).getFullYear() ? day : `${day} ${d.getFullYear()}`
}

/** A span of days, as Runs and Charts both show it: "6 Nov – 13 Nov". A bare
 *  "YYYY-MM-DD" is read as a local date. Pure, for tests. */
export function fmtDayRange(from: string | null | undefined, to: string | null | undefined, now = Date.now()): string {
  const local = (v: string | null | undefined) => (v && /^\d{4}-\d{2}-\d{2}$/.test(v) ? `${v}T00:00:00` : v)
  const a = fmtDay(local(from), now)
  const b = fmtDay(local(to), now)
  if (!a) return b
  return !b || a === b ? a : `${a} – ${b}`
}

/** A clock on screen, with the full timestamp on hover. */
export function Clock({ t, seconds = true, style }: { t: string | number | Date | null | undefined; seconds?: boolean; style?: React.CSSProperties }) {
  const full = fullWhen(t)
  return <time dateTime={toDate(t)?.toISOString()} title={full || undefined} style={style}>{fmtClock(t, seconds)}</time>
}

/** "1 file" / "3 files". */
export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

/** A moment on screen: the short form, the full timestamp on hover. */
export function When({ t, style }: { t: string | number | Date | null | undefined; style?: React.CSSProperties }) {
  const full = fullWhen(t)
  return <time dateTime={toDate(t)?.toISOString()} title={full || undefined} style={style}>{fmtWhen(t)}</time>
}

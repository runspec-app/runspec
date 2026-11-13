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

/** A moment on screen: the short form, the full timestamp on hover. */
export function When({ t, style }: { t: string | number | Date | null | undefined; style?: React.CSSProperties }) {
  const full = fullWhen(t)
  return <time dateTime={toDate(t)?.toISOString()} title={full || undefined} style={style}>{fmtWhen(t)}</time>
}

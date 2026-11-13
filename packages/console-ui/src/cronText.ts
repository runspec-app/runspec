// A plain reading of a cron expression ("Weekdays at 09:30") for the schedule
// tables, so an operator doesn't have to parse "30 9 * * 1-5" in their head.
// Covers the shapes people actually write; anything else returns null and the
// table shows the raw expression alone.

const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
const NICKNAMES: Record<string, string> = {
  '@yearly': 'Every year on 1 January at 00:00', '@annually': 'Every year on 1 January at 00:00',
  '@monthly': 'Monthly on the 1st at 00:00', '@weekly': 'Sundays at 00:00',
  '@daily': 'Daily at 00:00', '@midnight': 'Daily at 00:00', '@hourly': 'Every hour',
  '@reboot': 'When the host starts',
}

const num = (s: string, lo: number, hi: number): number | null => {
  if (!/^\d+$/.test(s)) return null
  const n = Number(s)
  return n >= lo && n <= hi ? n : null
}
const pad = (n: number) => String(n).padStart(2, '0')
const ordinal = (n: number) => {
  const s = n % 100 >= 11 && n % 100 <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] ?? 'th'
  return `${n}${s}`
}

const DAY_NAMES = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']
const dayNum = (s: string): number | null => {
  const i = DAY_NAMES.indexOf(s.toLowerCase())
  return i >= 0 ? i : num(s, 0, 7)
}

function days(field: string): string | null {
  if (field === '*') return null
  const f = field.toLowerCase()
  if (f === '1-5' || f === 'mon-fri') return 'Weekdays'
  if (f === '0,6' || f === '6,0' || f === 'sat,sun' || f === 'sun,sat') return 'Weekends'
  const range = f.match(/^(\w+)-(\w+)$/)
  if (range) {
    const a = dayNum(range[1]), b = dayNum(range[2])
    return a === null || b === null ? '' : `${DAYS[a % 7]} to ${DAYS[b % 7]}`
  }
  const parts = f.split(',')
  const names: string[] = []
  for (const p of parts) {
    const d = dayNum(p)
    if (d === null) return ''
    names.push(DAYS[d % 7] + 's')
  }
  return names.length === 1 ? names[0] : names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1]
}

/** "Daily at 02:00" for "0 2 * * *"; null when the expression isn't one of the
 *  common shapes (the caller then shows only the raw text). Pure, for tests. */
export function describeCron(expr: string): string | null {
  const e = expr.trim()
  if (!e) return null
  if (e.startsWith('@')) return NICKNAMES[e.toLowerCase()] ?? null
  const f = e.split(/\s+/)
  if (f.length !== 5) return null
  const [mi, hr, dom, mon, dow] = f
  if (mon !== '*') return null

  // Every N minutes / every N hours.
  const stepM = mi.match(/^\*\/(\d+)$/)
  if (stepM && hr === '*' && dom === '*' && dow === '*') {
    const n = Number(stepM[1])
    return n === 1 ? 'Every minute' : `Every ${n} minutes`
  }
  if (mi === '*' && hr === '*' && dom === '*' && dow === '*') return 'Every minute'
  const m = num(mi, 0, 59)
  if (m === null) return null
  const stepH = hr.match(/^\*\/(\d+)$/)
  if (stepH && dom === '*' && dow === '*') {
    const n = Number(stepH[1])
    return n === 1 ? `Every hour at :${pad(m)}` : `Every ${n} hours at :${pad(m)}`
  }
  if (hr === '*' && dom === '*' && dow === '*') return m === 0 ? 'Every hour' : `Every hour at :${pad(m)}`

  const hours = hr.split(',').map(h => num(h, 0, 23))
  if (hours.some(h => h === null)) return null
  const at = (hours as number[]).map(h => `${pad(h)}:${pad(m)}`).join(', ')

  const day = days(dow)
  if (day === '') return null
  if (dom === '*') return day ? `${day} at ${at}` : `Daily at ${at}`
  const d = num(dom, 1, 31)
  if (d === null || day) return null
  return `Monthly on the ${ordinal(d)} at ${at}`
}

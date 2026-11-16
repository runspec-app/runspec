import { describe, expect, it } from 'vitest'
import { rotaAllows, rotaGates, rotaSummary } from './rota'
import type { Rota } from './bridge'

const WEEK: Rota = {
  enabled: true, source: 'console',
  mon: { on: true, start: '09:00', end: '17:30' }, tue: { on: true, start: '09:00', end: '17:30' },
  wed: { on: true, start: '09:00', end: '17:30' }, thu: { on: true, start: '09:00', end: '17:30' },
  fri: { on: true, start: '09:00', end: '16:00' }, sat: { on: false }, sun: { on: false },
}
// 2026-11-16 is a Monday.
const at = (day: number, hh: number, mm = 0) => new Date(2026, 10, 16 + day, hh, mm)

describe('rotaAllows()', () => {
  it('is null when working hours are off or come from Outlook', () => {
    expect(rotaAllows({ ...WEEK, enabled: false }, at(0, 2))).toBeNull()
    expect(rotaAllows({ ...WEEK, source: 'outlook' }, at(0, 2))).toBeNull()
    expect(rotaGates({ ...WEEK, source: 'outlook' })).toBe(false)
  })
  it('skips a 02:00 run and a weekend run, allows one inside the hours', () => {
    expect(rotaAllows(WEEK, at(0, 2))).toBe(false)
    expect(rotaAllows(WEEK, at(5, 10))).toBe(false)
    expect(rotaAllows(WEEK, at(0, 9))).toBe(true)
    expect(rotaAllows(WEEK, at(4, 16))).toBe(false)  // Friday ends at 16:00
  })
  it('handles a shift that runs past midnight', () => {
    const nights: Rota = { enabled: true, source: 'console', mon: { on: true, start: '22:00', end: '06:00' } }
    expect(rotaAllows(nights, at(0, 23))).toBe(true)
    expect(rotaAllows(nights, at(1, 5))).toBe(true)
    expect(rotaAllows(nights, at(1, 7))).toBe(false)
  })
})

describe('rotaSummary()', () => {
  it('joins days that share hours', () => {
    expect(rotaSummary(WEEK)).toBe('Mon–Thu 09:00–17:30, Fri 09:00–16:00')
  })
})

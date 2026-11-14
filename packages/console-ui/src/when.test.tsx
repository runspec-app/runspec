import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { fmtClock, fmtDay, fmtDayRange, fmtWhen, fullWhen, plural, When } from './when'

const now = new Date(2026, 10, 13, 19, 53, 0).getTime() // 13 Nov 2026 19:53 local

describe('clock, day and range', () => {
  it('shows a 24h clock whatever the locale', () => {
    expect(fmtClock(new Date(2026, 10, 13, 7, 5, 9))).toBe('07:05:09')
    expect(fmtClock(new Date(2026, 10, 13, 19, 53, 0), false)).toBe('19:53')
    expect(fmtClock(null)).toBe('')
  })

  it('shows days and spans the same way for Runs and Charts', () => {
    expect(fmtDay(new Date(2026, 10, 6), now)).toBe('6 Nov')
    expect(fmtDay(new Date(2024, 4, 29), now)).toBe('29 May 2024')
    expect(fmtDayRange('2026-11-06', '2026-11-13', now)).toBe('6 Nov – 13 Nov')
    expect(fmtDayRange('2026-11-13T08:00:00Z', '2026-11-13T09:00:00Z', now)).toBe('13 Nov')
    expect(fmtDayRange('', '2026-11-13', now)).toBe('13 Nov')
  })

  it('says one file, not file(s)', () => {
    expect(plural(1, 'file')).toBe('1 file')
    expect(plural(3, 'file')).toBe('3 files')
  })
})

describe('fmtWhen', () => {
  it.each([
    [now - 10_000, 'just now'],
    [now - 5 * 60_000, '5m ago'],
    [now + 5 * 60_000, 'in 5m'],
    [now - 3 * 3_600_000, '3h ago'],
    [now + 3 * 3_600_000, 'in 3h'],
    [new Date(2026, 10, 10, 8, 5).getTime(), '10 Nov 08:05'],
    [new Date(2024, 4, 29, 14, 0).getTime(), '29 May 2024'],
  ])('%s → %s', (t, out) => {
    expect(fmtWhen(t, now)).toBe(out)
  })

  it('accepts ISO strings and handles missing or bad input', () => {
    expect(fmtWhen(new Date(2026, 10, 10, 8, 5).toISOString(), now)).toBe('10 Nov 08:05')
    expect(fmtWhen(null, now)).toBe('—')
    expect(fmtWhen('', now)).toBe('—')
    expect(fmtWhen('not a date', now)).toBe('—')
  })
})

describe('When', () => {
  it('shows the short form with the full timestamp on hover', () => {
    const t = new Date(2024, 4, 29, 14, 0, 7)
    render(<When t={t} />)
    const el = screen.getByText('29 May 2024')
    expect(el.tagName).toBe('TIME')
    expect(el.getAttribute('title')).toBe(fullWhen(t))
    expect(el.getAttribute('title')).toContain('29 May 2024, 14:00:07')
  })
})

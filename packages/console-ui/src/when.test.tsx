import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { fmtWhen, fullWhen, When } from './when'

const now = new Date(2026, 10, 13, 19, 53, 0).getTime() // 13 Nov 2026 19:53 local

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

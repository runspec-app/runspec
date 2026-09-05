import { describe, it, expect } from 'vitest'
import { parseEmails } from './ImproveView'

describe('parseEmails', () => {
  it('splits a comma/semicolon/space separated string', () => {
    expect(parseEmails('a@x.com, b@x.com')).toEqual(['a@x.com', 'b@x.com'])
    expect(parseEmails('a@x.com;b@x.com  c@x.com')).toEqual(['a@x.com', 'b@x.com', 'c@x.com'])
  })
  it('accepts an array and trims/drops blanks', () => {
    expect(parseEmails([' a@x.com ', '', 'b@x.com'])).toEqual(['a@x.com', 'b@x.com'])
  })
  it('returns [] for missing / non-string / non-array', () => {
    expect(parseEmails(undefined)).toEqual([])
    expect(parseEmails(null)).toEqual([])
    expect(parseEmails(42)).toEqual([])
    expect(parseEmails('')).toEqual([])
  })
})

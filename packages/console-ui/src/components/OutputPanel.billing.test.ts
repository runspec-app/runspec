import { describe, it, expect } from 'vitest'
import { billingLabel, fmtUnits } from './OutputPanel'

const usage = { input: 100, output: 20, cacheRead: 900, cacheCreation: 0 }

describe('billingLabel', () => {
  it('is null for a token-priced provider', () => {
    expect(billingLabel(usage)).toBeNull()
  })

  it('shows Copilot AI credits and premium requests for the turn', () => {
    const b = billingLabel({ ...usage, aiCredits: 0.4234, premiumRequests: 1 })
    expect(b?.text).toBe('0.42 credits · 1 premium req')
    expect(b?.tip).toContain('AI credits')
    expect(b?.tip).toContain('premium requests')
  })

  it('shows whichever one Copilot reported', () => {
    expect(billingLabel({ ...usage, premiumRequests: 1.5 })?.text).toBe('1.5 premium req')
    expect(billingLabel({ ...usage, aiCredits: 3 })?.text).toBe('3 credits')
  })
})

describe('fmtUnits', () => {
  it('keeps small amounts readable', () => {
    expect(fmtUnits(0.000123)).toBe('0.00012')
    expect(fmtUnits(2.345)).toBe('2.35')
    expect(fmtUnits(1234.6)).toBe((1235).toLocaleString())
  })
})

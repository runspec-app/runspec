import { describe, it, expect } from 'vitest'
import { denyAppliedEverywhere } from './denylistPending'

describe('denyAppliedEverywhere', () => {
  it('is true only when every venv tried said ok', () => {
    expect(denyAppliedEverywhere('runspec deny all — 2/2 venv(s) ok across 1 host(s):\n✓ a')).toBe(true)
    expect(denyAppliedEverywhere('runspec deny all — 1/2 venv(s) ok across 1 host(s):')).toBe(false)
    expect(denyAppliedEverywhere('runspec deny all — 0/0 venv(s) ok across 0 host(s):')).toBe(false)
    expect(denyAppliedEverywhere('error: unreachable')).toBe(false)
  })
})

import { describe, it, expect } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { chartDays, useHistoryRange, setHistoryRange } from './historyRange'

describe('history range', () => {
  it('is shared between every view that reads it', () => {
    const a = renderHook(() => useHistoryRange())
    const b = renderHook(() => useHistoryRange())
    act(() => { a.result.current[1](30) })
    expect(b.result.current[0]).toBe(30)
    act(() => { setHistoryRange(7) })
  })
  it('caps charts at the 30 days the cache keeps', () => {
    expect(chartDays(7)).toBe(7)
    expect(chartDays(90)).toBe(30)
    expect(chartDays(0)).toBe(30)
  })
})

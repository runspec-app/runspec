/**
 * useWaitingCue: holds the backend's looping "waiting" ping while a badge needs
 * the operator (trigger approvals / queue callers, or any live confirm card),
 * re-asserts while it stays, and releases it when everything clears.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

import { useWaitingCue, waitingCueActive, WAITING_REASSERT_MS } from './useWaitingCue'

const { setWaitingCue } = vi.hoisted(() => ({ setWaitingCue: vi.fn() }))

vi.mock('./bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => (p === 'set_waiting_cue' ? setWaitingCue : vi.fn()),
  }),
}))

const fire = (name: string, detail: unknown) =>
  act(() => { window.dispatchEvent(new CustomEvent(name, { detail })) })

beforeEach(() => {
  vi.useFakeTimers()
  setWaitingCue.mockReset().mockResolvedValue(true)
})
afterEach(() => { vi.useRealTimers() })

describe('waitingCueActive', () => {
  it('is on when either source has something waiting', () => {
    expect(waitingCueActive(0, 0)).toBe(false)
    expect(waitingCueActive(2, 0)).toBe(true)
    expect(waitingCueActive(0, 1)).toBe(true)
  })
})

describe('useWaitingCue', () => {
  it('holds the ping while the Triggers badge has a count and releases it after', () => {
    const { result, rerender } = renderHook(({ n }) => useWaitingCue(n), { initialProps: { n: 0 } })
    expect(result.current).toBe(false)
    expect(setWaitingCue).toHaveBeenLastCalledWith(false)

    rerender({ n: 1 })
    expect(result.current).toBe(true)
    expect(setWaitingCue).toHaveBeenLastCalledWith(true)

    rerender({ n: 0 })
    expect(result.current).toBe(false)
    expect(setWaitingCue).toHaveBeenLastCalledWith(false)
  })

  it('tracks live confirm cards until resolved or ended', () => {
    const { result } = renderHook(() => useWaitingCue(0))
    fire('runspec:tool_confirm', { id: 'a' })
    fire('runspec:tool_confirm', { id: 'b' })
    expect(result.current).toBe(true)
    fire('runspec:confirm_resolved', { id: 'a' })
    expect(result.current).toBe(true)
    fire('runspec:run_end', { id: 'b' })
    expect(result.current).toBe(false)
    expect(setWaitingCue).toHaveBeenLastCalledWith(false)
  })

  it('re-asserts while active so the backend lease never lapses', () => {
    renderHook(() => useWaitingCue(1))
    setWaitingCue.mockClear()
    act(() => { vi.advanceTimersByTime(WAITING_REASSERT_MS * 2) })
    expect(setWaitingCue).toHaveBeenCalledTimes(2)
    expect(setWaitingCue).toHaveBeenLastCalledWith(true)
  })

  it('releases the ping on unmount', () => {
    const { unmount } = renderHook(() => useWaitingCue(3))
    unmount()
    expect(setWaitingCue).toHaveBeenLastCalledWith(false)
  })
})

/**
 * finishToolSegment — pairing a runspec:tool_end with the right tool card.
 *
 * The agent loop now runs independent runnable calls from one model response
 * concurrently, so several cards with the SAME tool name (e.g. five
 * run_runnable calls, one per host) can be running at once. Each event carries
 * the model's `call_id`; a result must finish only the card it belongs to.
 */
import { describe, it, expect, vi } from 'vitest'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn() }),
}))

import { confirmOwnsSegment, finishToolSegment } from './OutputPanel'
import type { BlockSegment } from './OutputPanel'

const running = (name: string, callId?: string, startedAt = 1000): BlockSegment => ({
  kind: 'tool',
  entry: { name, ...(callId ? { callId } : {}), input: {}, running: true, startedAt },
})

const entryOf = (seg: BlockSegment) => {
  if (seg.kind !== 'tool') throw new Error('not a tool segment')
  return seg.entry
}

describe('finishToolSegment', () => {
  it('finishes only the card whose call_id matches, even when names collide', () => {
    const segs = [running('run_runnable', 'ta'), running('run_runnable', 'tb'), running('run_runnable', 'tc')]
    const out = finishToolSegment(segs, { call_id: 'tb', tool_name: 'run_runnable', output: 'out-b' }, 1500)
    expect(out.map(s => entryOf(s).running)).toEqual([true, false, true])
    expect(entryOf(out[1]).output).toBe('out-b')
    expect(entryOf(out[1]).durationMs).toBe(500)
    expect(entryOf(out[0]).output).toBeUndefined()
  })

  it('pairs results that arrive out of order', () => {
    let segs = [running('run_runnable', 'ta'), running('run_runnable', 'tb')]
    segs = finishToolSegment(segs, { call_id: 'tb', tool_name: 'run_runnable', output: 'out-b' })
    segs = finishToolSegment(segs, { call_id: 'ta', tool_name: 'run_runnable', output: 'out-a' })
    expect(segs.map(s => entryOf(s).output)).toEqual(['out-a', 'out-b'])
  })

  it('without a call_id (older backend) finishes only the FIRST running same-name card', () => {
    const segs = [running('disk-usage'), running('disk-usage')]
    const out = finishToolSegment(segs, { tool_name: 'disk-usage', output: 'x' })
    expect(out.map(s => entryOf(s).running)).toEqual([false, true])
  })

  it('keeps the confirm-gate badge and truncation info', () => {
    const out = finishToolSegment([running('restart', 't1')], {
      call_id: 't1', tool_name: 'restart', output: 'ok', output_chars: 9000,
      truncated: { orig_len: 9000, cap: 2000 }, operator_decision: 'approved',
    })
    expect(entryOf(out[0])).toMatchObject({ operatorDecision: 'approved', outputChars: 9000, truncated: { orig_len: 9000, cap: 2000 } })
  })

  it('leaves segments untouched when nothing matches', () => {
    const segs: BlockSegment[] = [{ kind: 'text', text: 'hi' }, running('a', 't1')]
    expect(finishToolSegment(segs, { call_id: 'zz', tool_name: 'a', output: 'x' })).toBe(segs)
  })
})

describe('confirmOwnsSegment', () => {
  const pc = { request_id: 'r', id: 'c', tool_name: 'run_runnable', tool_input: {}, autonomy: 'confirm' }
  const entry = (over: Record<string, unknown>) => ({ name: 'run_runnable', input: {}, running: true, ...over })

  it('matches by call id when both sides have one', () => {
    expect(confirmOwnsSegment({ ...pc, call_id: 'b' }, entry({ callId: 'b' }))).toBe(true)
    expect(confirmOwnsSegment({ ...pc, call_id: 'b' }, entry({ callId: 'a' }))).toBe(false)
  })

  it('falls back to the tool name without a call id (older backend)', () => {
    expect(confirmOwnsSegment(pc, entry({ callId: 'a' }))).toBe(true)
    expect(confirmOwnsSegment(pc, entry({ name: 'other' }))).toBe(false)
  })

  it('never claims a finished card or a missing confirm', () => {
    expect(confirmOwnsSegment({ ...pc, call_id: 'b' }, entry({ callId: 'b', running: false }))).toBe(false)
    expect(confirmOwnsSegment(null, entry({}))).toBe(false)
  })
})

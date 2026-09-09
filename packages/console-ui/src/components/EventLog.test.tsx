import { describe, it, expect } from 'vitest'
import { disposition } from './EventLog'
import { COLUMN_COLOR } from './runShared'
import type { TriggerEvent } from '../bridge'

const DIM = '#999'

function ev(partial: Partial<TriggerEvent>): TriggerEvent {
  return {
    id: 'e', source: 'outlook', type: 'new_mail', summary: 's',
    matched: [], skipped: [], ts: 0, ...partial,
  }
}

describe('EventLog disposition', () => {
  it('reports a fired event with the matched triggers', () => {
    const d = disposition(ev({ matched: ['vip'] }), DIM)
    expect(d.text).toBe('fired: vip')
    expect(d.color).toBe(COLUMN_COLOR.done)
  })

  it('labels a lock skip distinctly from a rota skip', () => {
    const locked = disposition(ev({ skipped: ['vip'], skip_reasons: { vip: 'screen locked' } }), DIM)
    expect(locked.text).toBe('skipped (screen locked): vip')
    expect(locked.color).toBe(COLUMN_COLOR.awaiting_human)

    const rota = disposition(ev({ skipped: ['vip'], skip_reasons: { vip: 'working hours' } }), DIM)
    expect(rota.text).toBe('skipped (working hours): vip')
  })

  it('groups multiple skipped triggers by reason', () => {
    const d = disposition(
      ev({ skipped: ['a', 'b', 'c'], skip_reasons: { a: 'screen locked', b: 'screen locked', c: 'working hours' } }),
      DIM,
    )
    expect(d.text).toContain('skipped (screen locked): a, b')
    expect(d.text).toContain('skipped (working hours): c')
  })

  it('falls back to working hours for an older record with no skip_reasons', () => {
    const d = disposition(ev({ skipped: ['vip'] }), DIM)
    expect(d.text).toBe('skipped (working hours): vip')
  })

  it('labels a once_per_thread repeat as already handled (not "no match")', () => {
    const d = disposition(ev({ deduped: ['vip'] }), DIM)
    expect(d.text).toBe('skipped (already handled): vip')
    expect(d.color).toBe(COLUMN_COLOR.awaiting_human)
  })

  it('reports no match when nothing fired, skipped, or deduped', () => {
    const d = disposition(ev({}), DIM)
    expect(d.text).toBe('no match')
    expect(d.color).toBe(DIM)
  })
})

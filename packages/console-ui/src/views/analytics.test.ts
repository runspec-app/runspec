import { describe, it, expect } from 'vitest'
import {
  filterBuckets, computeKpis, runsOverTime, successRateOverTime,
  durationTrend, topRunnables, operatorBreakdown, initiatedBreakdown,
} from './analytics'
import type { AnalyticsBucket, AnalyticsDaily } from '../bridge'
import { mockApi } from '../bridge/mock'

function bucket(p: Partial<AnalyticsBucket>): AnalyticsBucket {
  return {
    date: '2026-06-01', host: 'local', group: 'ops', runnable: 'backup',
    operator: 'alice', initiatedBy: 'user', autonomy: 'confirm',
    total: 1, success: 1, failure: 0, durationMsSum: 100,
    events: { DEBUG: 0, INFO: 1, WARNING: 0, ERROR: 0, CRITICAL: 0 },
    ...p,
  }
}

describe('filterBuckets', () => {
  const buckets = [
    bucket({ group: 'ops', operator: 'alice', initiatedBy: 'user' }),
    bucket({ group: 'core', operator: 'bob', initiatedBy: 'llm' }),
    bucket({ group: 'ops', operator: 'bob', initiatedBy: 'user' }),
  ]

  it('returns everything when no filter is set', () => {
    expect(filterBuckets(buckets, [], [], 'all')).toHaveLength(3)
  })

  it('filters by group', () => {
    expect(filterBuckets(buckets, ['ops'], [], 'all')).toHaveLength(2)
  })

  it('filters by operator', () => {
    expect(filterBuckets(buckets, [], ['bob'], 'all')).toHaveLength(2)
  })

  it('filters by initiatedBy', () => {
    expect(filterBuckets(buckets, [], [], 'llm')).toHaveLength(1)
  })

  it('ANDs multiple filters together', () => {
    expect(filterBuckets(buckets, ['ops'], ['bob'], 'user')).toHaveLength(1)
  })
})

describe('computeKpis', () => {
  it('sums totals and derives the success rate', () => {
    const k = computeKpis([
      bucket({ total: 4, success: 3, failure: 1 }),
      bucket({ total: 6, success: 6, failure: 0 }),
    ])
    expect(k).toEqual({ total: 10, success: 9, failure: 1, rate: 90 })
  })

  it('reports a zero rate (not NaN) when there are no runs', () => {
    expect(computeKpis([])).toEqual({ total: 0, success: 0, failure: 0, rate: 0 })
  })
})

describe('runsOverTime', () => {
  const daily: AnalyticsDaily[] = [
    { date: '2026-06-01', total: 0, success: 0, failure: 0, p50DurationMs: 0, p95DurationMs: 0 },
    { date: '2026-06-02', total: 0, success: 0, failure: 0, p50DurationMs: 0, p95DurationMs: 0 },
  ]

  it('emits a success and a failure point per day, zero-filling gap days', () => {
    const rows = runsOverTime(daily, [
      bucket({ date: '2026-06-01', success: 2, failure: 1 }),
    ])
    expect(rows).toHaveLength(4) // 2 days x {success, failure}
    expect(rows.find(r => r.date === '2026-06-01' && r.status === 'success')!.value).toBe(2)
    expect(rows.find(r => r.date === '2026-06-01' && r.status === 'failure')!.value).toBe(1)
    // 06-02 has no buckets → zero-filled, not dropped.
    expect(rows.find(r => r.date === '2026-06-02' && r.status === 'success')!.value).toBe(0)
  })

  it('aggregates multiple buckets on the same day', () => {
    const rows = runsOverTime([daily[0]], [
      bucket({ date: '2026-06-01', success: 1, failure: 0 }),
      bucket({ date: '2026-06-01', success: 0, failure: 3 }),
    ])
    expect(rows.find(r => r.status === 'success')!.value).toBe(1)
    expect(rows.find(r => r.status === 'failure')!.value).toBe(3)
  })
})

describe('successRateOverTime', () => {
  it('computes per-day rate and yields null for empty days', () => {
    const daily: AnalyticsDaily[] = [
      { date: '2026-06-01', total: 0, success: 0, failure: 0, p50DurationMs: 0, p95DurationMs: 0 },
      { date: '2026-06-02', total: 0, success: 0, failure: 0, p50DurationMs: 0, p95DurationMs: 0 },
    ]
    const rows = successRateOverTime(daily, [
      bucket({ date: '2026-06-01', total: 4, success: 3, failure: 1 }),
    ])
    expect(rows[0]).toEqual({ date: '2026-06-01', rate: 75 })
    expect(rows[1]).toEqual({ date: '2026-06-02', rate: null })
  })
})

describe('durationTrend', () => {
  it('flattens daily p50/p95 into long form', () => {
    const rows = durationTrend([
      { date: '2026-06-01', total: 1, success: 1, failure: 0, p50DurationMs: 100, p95DurationMs: 900 },
    ])
    expect(rows).toEqual([
      { date: '2026-06-01', metric: 'p50', value: 100 },
      { date: '2026-06-01', metric: 'p95', value: 900 },
    ])
  })
})

describe('topRunnables', () => {
  it('ranks by total runs and respects the limit', () => {
    const buckets = [
      bucket({ runnable: 'backup', total: 2 }),
      bucket({ runnable: 'deploy', total: 5 }),
      bucket({ runnable: 'backup', total: 3 }),
      bucket({ runnable: 'purge', total: 1 }),
    ]
    expect(topRunnables(buckets, 2)).toEqual([
      { runnable: 'backup', runs: 5 },
      { runnable: 'deploy', runs: 5 },
    ].sort((a, b) => b.runs - a.runs).slice(0, 2))
    // backup (2+3=5) and deploy (5) tie at the top; purge is dropped by limit.
    const top = topRunnables(buckets, 2)
    expect(top.map(t => t.runnable).sort()).toEqual(['backup', 'deploy'])
  })
})

describe('breakdowns', () => {
  it('operatorBreakdown sums per operator, descending', () => {
    const out = operatorBreakdown([
      bucket({ operator: 'alice', total: 1 }),
      bucket({ operator: 'bob', total: 4 }),
      bucket({ operator: 'alice', total: 2 }),
    ])
    expect(out[0]).toEqual({ type: 'bob', value: 4 })
    expect(out.find(o => o.type === 'alice')!.value).toBe(3)
  })

  it('initiatedBreakdown splits user vs agent', () => {
    const out = initiatedBreakdown([
      bucket({ initiatedBy: 'user', total: 3 }),
      bucket({ initiatedBy: 'llm', total: 2 }),
    ])
    expect(out).toEqual([{ type: 'user', value: 3 }, { type: 'agent (LLM)', value: 2 }])
  })
})

// Black-box check of the browser mock's aggregator — guards the data contract
// the charts render against in `npm run dev`.
describe('mock get_analytics', () => {
  it('returns a self-consistent, zero-filled multiday dataset', async () => {
    const d = await mockApi.get_analytics(['all'], 30)
    expect(d.totalRuns).toBe(d.successCount + d.failureCount)
    expect(d.daily).toHaveLength(30)               // zero-filled across the window
    expect(d.dimensions.hosts.length).toBeGreaterThan(0)
    expect(d.durationMs.p95).toBeGreaterThanOrEqual(d.durationMs.p50)
    // daily totals reconcile with the headline count
    expect(d.daily.reduce((n, x) => n + x.total, 0)).toBe(d.totalRuns)
  })

  it('honours the host filter', async () => {
    const d = await mockApi.get_analytics(['local'], 30)
    expect(d.buckets.every(b => b.host === 'local')).toBe(true)
  })

  it('honours the runnable filter', async () => {
    const d = await mockApi.get_analytics(['all'], 30, 'backup')
    expect(d.buckets.every(b => b.runnable === 'backup')).toBe(true)
  })
})

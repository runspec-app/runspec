// Pure aggregation/filter helpers for the Analytics view.
//
// These are split out of AnalyticsView so the drill/filter maths can be
// unit-tested without rendering React. The component wraps each in a useMemo;
// everything here is side-effect-free and takes plain data in, plain data out.

import type { AnalyticsBucket, AnalyticsDaily } from '../bridge'

export type InitiatedFilter = 'all' | 'user' | 'llm'

/** Client-side drill: keep buckets matching the active filter selection. */
export function filterBuckets(
  buckets: AnalyticsBucket[],
  groups: string[],
  operators: string[],
  initiatedBy: InitiatedFilter,
): AnalyticsBucket[] {
  return buckets.filter(b =>
    (groups.length === 0 || groups.includes(b.group)) &&
    (operators.length === 0 || operators.includes(b.operator)) &&
    (initiatedBy === 'all' || b.initiatedBy === initiatedBy)
  )
}

export interface Kpis {
  total: number
  success: number
  failure: number
  rate: number   // success %, 0 when no runs
}

export function computeKpis(buckets: AnalyticsBucket[]): Kpis {
  let total = 0, success = 0, failure = 0
  for (const b of buckets) { total += b.total; success += b.success; failure += b.failure }
  return { total, success, failure, rate: total > 0 ? (success / total) * 100 : 0 }
}

export interface RunsPoint { date: string; status: string; value: number }

/**
 * Success-vs-failure runs per day, recomputed from the filtered buckets and
 * zero-filled against the server's day axis so the series stays continuous.
 */
export function runsOverTime(daily: AnalyticsDaily[], buckets: AnalyticsBucket[]): RunsPoint[] {
  const byDay = new Map<string, { success: number; failure: number }>()
  for (const b of buckets) {
    const d = byDay.get(b.date) ?? { success: 0, failure: 0 }
    d.success += b.success; d.failure += b.failure
    byDay.set(b.date, d)
  }
  const rows: RunsPoint[] = []
  for (const day of daily) {
    const d = byDay.get(day.date) ?? { success: 0, failure: 0 }
    rows.push({ date: day.date, status: 'success', value: d.success })
    rows.push({ date: day.date, status: 'failure', value: d.failure })
  }
  return rows
}

export interface RatePoint { date: string; rate: number | null }

/** Per-day success rate (%), null on days with no runs so the line breaks. */
export function successRateOverTime(daily: AnalyticsDaily[], buckets: AnalyticsBucket[]): RatePoint[] {
  const byDay = new Map<string, { total: number; success: number }>()
  for (const b of buckets) {
    const d = byDay.get(b.date) ?? { total: 0, success: 0 }
    d.total += b.total; d.success += b.success
    byDay.set(b.date, d)
  }
  return daily.map(day => {
    const d = byDay.get(day.date)
    return { date: day.date, rate: d && d.total > 0 ? Math.round((d.success / d.total) * 1000) / 10 : null }
  })
}

export interface MetricPoint { date: string; metric: string; value: number }

/** p50/p95 duration trend straight from the server's daily series. */
export function durationTrend(daily: AnalyticsDaily[]): MetricPoint[] {
  const rows: MetricPoint[] = []
  for (const day of daily) {
    rows.push({ date: day.date, metric: 'p50', value: day.p50DurationMs })
    rows.push({ date: day.date, metric: 'p95', value: day.p95DurationMs })
  }
  return rows
}

export interface RunnableCount { runnable: string; runs: number }

export function topRunnables(buckets: AnalyticsBucket[], limit = 12): RunnableCount[] {
  const m = new Map<string, number>()
  for (const b of buckets) m.set(b.runnable, (m.get(b.runnable) ?? 0) + b.total)
  return [...m.entries()]
    .map(([runnable, runs]) => ({ runnable, runs }))
    .sort((a, b) => b.runs - a.runs)
    .slice(0, limit)
}

export interface CategoryValue { type: string; value: number }

export function operatorBreakdown(buckets: AnalyticsBucket[]): CategoryValue[] {
  const m = new Map<string, number>()
  for (const b of buckets) m.set(b.operator || '—', (m.get(b.operator || '—') ?? 0) + b.total)
  return [...m.entries()].map(([type, value]) => ({ type, value })).sort((a, b) => b.value - a.value)
}

export function initiatedBreakdown(buckets: AnalyticsBucket[]): CategoryValue[] {
  let user = 0, llm = 0
  for (const b of buckets) b.initiatedBy === 'llm' ? (llm += b.total) : (user += b.total)
  return [{ type: 'user', value: user }, { type: 'agent (LLM)', value: llm }]
}

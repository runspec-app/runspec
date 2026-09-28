/**
 * AnalyticsView: the window is capped at 30 days (the History cache's window —
 * no 90d option) and a note shows when some hosts' runs predate the log-event /
 * exception fields in the runs index (runspec < 0.64).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

import { AnalyticsView } from './AnalyticsView'
import type { AnalyticsData } from '../bridge'

const { getAnalytics } = vi.hoisted(() => ({ getAnalytics: vi.fn() }))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => (p === 'get_analytics' ? getAnalytics : vi.fn()),
  }),
}))

// The charts render to canvas, which jsdom lacks — stub them out.
vi.mock('@ant-design/plots', () => ({
  Area: () => null, Line: () => null, Column: () => null, Pie: () => null,
}))

function data(over: Partial<AnalyticsData> = {}): AnalyticsData {
  return {
    since: '2026-08-30', until: '2026-09-28',
    totalRuns: 0, successCount: 0, failureCount: 0,
    durationMs: { p50: 0, p95: 0, p99: 0, max: 0, mean: 0 },
    daily: [], buckets: [], exceptions: [],
    dimensions: { hosts: [], groups: [], runnables: [], operators: [], initiatedBy: ['user', 'llm'], runbooks: [] },
    partial: false, errors: [],
    ...over,
  } as AnalyticsData
}

const PROPS = { selectedHost: 'web-01', activeScope: [], active: true }

beforeEach(() => {
  getAnalytics.mockReset().mockResolvedValue(data())
})

describe('AnalyticsView', () => {
  it('reads a 30-day window and offers no range beyond it', async () => {
    render(<AnalyticsView {...PROPS} />)
    expect(await screen.findByText('2026-08-30 → 2026-09-28')).toBeTruthy()
    expect(getAnalytics).toHaveBeenCalledWith(['web-01'], 30, undefined)
    expect(screen.getByText('30d')).toBeTruthy()
    expect(screen.queryByText('90d')).toBeNull()
  })

  it('notes hosts whose runs lack log-event counts in the index', async () => {
    getAnalytics.mockResolvedValue(data({ eventsUnknown: ['web-01'] }))
    render(<AnalyticsView {...PROPS} />)
    expect(await screen.findByText('Log events and exceptions are incomplete for some hosts')).toBeTruthy()
    expect(screen.getByText(/Some runs on web-01 were recorded by a runspec older than 0\.64/)).toBeTruthy()
  })

  it('shows no note when every host is complete', async () => {
    render(<AnalyticsView {...PROPS} />)
    await screen.findByText('2026-08-30 → 2026-09-28')
    expect(screen.queryByText(/incomplete for some hosts/)).toBeNull()
  })
})

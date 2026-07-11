/**
 * HistoryView data loading: renders rows after the first fetch, refreshes on a
 * debounced runspec:run_end event (not on every keystroke), and slows its
 * fallback poll now that the backend read is cheap.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { HistoryView } from './HistoryView'
import type { HistoryRecord } from '../bridge'

const { getHistory, logsView } = vi.hoisted(() => ({
  getHistory: vi.fn(),
  logsView: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'get_history') return getHistory
      if (p === 'logs_view') return logsView
      return vi.fn()
    },
  }),
}))

function rec(over: Partial<HistoryRecord> = {}): HistoryRecord {
  return {
    id: 'row1', runId: 'run-1', runnable: 'backup', group: 'ops-tools',
    host: 'local', operator: 'alice', runAs: 'root', exitCode: 0,
    durationMs: 1200, ts: '2026-06-03T14:22:01+00:00', args: {}, logLines: [],
    ...over,
  }
}

const PROPS = {
  search: '', onSearchChange: () => {}, activeScope: [],
  onScopeToggle: () => {}, selectedHost: 'local', active: true,
}

beforeEach(() => {
  getHistory.mockReset().mockResolvedValue([rec()])
  logsView.mockReset().mockResolvedValue({ ok: true, records: [] })
})

afterEach(() => { vi.useRealTimers() })

describe('HistoryView', () => {
  it('renders rows after the first fetch', async () => {
    render(<HistoryView {...PROPS} />)
    expect(await screen.findByText('backup')).toBeTruthy()
    expect(getHistory).toHaveBeenCalledWith('local')
    expect(getHistory).toHaveBeenCalledTimes(1)
  })

  it('refetches (once, debounced) on a runspec:run_end event', async () => {
    vi.useFakeTimers()
    render(<HistoryView {...PROPS} />)
    await act(async () => { await Promise.resolve() })   // flush first fetch
    expect(getHistory).toHaveBeenCalledTimes(1)

    // A burst of run_end events collapses into a single debounced refetch.
    await act(async () => {
      window.dispatchEvent(new CustomEvent('runspec:run_end'))
      window.dispatchEvent(new CustomEvent('runspec:run_end'))
      await vi.advanceTimersByTimeAsync(800)
    })
    expect(getHistory).toHaveBeenCalledTimes(2)
  })

  it('does not poll within the (slowed) fallback window absent an event', async () => {
    vi.useFakeTimers()
    render(<HistoryView {...PROPS} />)
    await act(async () => { await Promise.resolve() })
    expect(getHistory).toHaveBeenCalledTimes(1)
    // Advance well past the old 8s cadence but under the new 25s poll — no refetch.
    await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
    expect(getHistory).toHaveBeenCalledTimes(1)
  })

  it('does not refetch when the search prop changes (client-side filter only)', async () => {
    const { rerender } = render(<HistoryView {...PROPS} />)
    await screen.findByText('backup')
    expect(getHistory).toHaveBeenCalledTimes(1)
    rerender(<HistoryView {...PROPS} search="nomatch" />)
    await waitFor(() => expect(screen.queryByText('backup')).toBeNull())
    expect(getHistory).toHaveBeenCalledTimes(1)   // no new fetch on filter
  })

  it('opens the full-log drill via logs_view when a row is expanded', async () => {
    logsView.mockResolvedValue({ ok: true, records: [{ ts: '2026-06-03T14:22:01+00:00', level: 'INFO', message: 'hello world' }] })
    render(<HistoryView {...PROPS} onRerun={() => {}} />)
    const user = userEvent.setup()
    // Expand the row (antd renders an expand toggle button per row).
    const expandBtn = await screen.findByLabelText(/Expand row/i)
    await act(async () => { await user.click(expandBtn) })
    const viewBtn = await screen.findByRole('button', { name: /View full log/i })
    await act(async () => { await user.click(viewBtn) })
    await waitFor(() => expect(logsView).toHaveBeenCalledWith('local', 'ops-tools', 'backup', undefined, undefined, 'run-1'))
    expect(await screen.findByText('hello world')).toBeTruthy()
  })
})

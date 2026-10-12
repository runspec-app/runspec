/**
 * HistoryView data loading: reads one host at a time (history_targets → a
 * get_history_slim per host) and renders each host as it arrives, so a slow or
 * failing host never blocks the rest; shows a per-host fetch strip; refreshes on
 * a debounced runspec:run_end; refetches when the range changes; and fetches a
 * run's log lines on demand when its row is expanded.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { HistoryView, accountNote, agoLabel, syncNote } from './HistoryView'
import type { HistoryRecord, RunningRun } from '../bridge'

const { historyTargets, getHistorySlim, getRunDetail, logsView, getRunningRuns, historySyncStatus } = vi.hoisted(() => ({
  historySyncStatus: vi.fn(),
  historyTargets: vi.fn(),
  getHistorySlim: vi.fn(),
  getRunDetail: vi.fn(),
  logsView: vi.fn(),
  getRunningRuns: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'history_targets') return historyTargets
      if (p === 'get_history_slim') return getHistorySlim
      if (p === 'get_run_detail') return getRunDetail
      if (p === 'logs_view') return logsView
      if (p === 'get_running_runs') return getRunningRuns
      if (p === 'history_sync_status') return historySyncStatus
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

function running(over: Partial<RunningRun> = {}): RunningRun {
  return {
    id: 'live1', runId: 'live-1', runnable: 'db-migrate', group: 'core',
    host: 'prod-1', operator: 'svc', runAs: 'svc', startTs: new Date(Date.now() - 5000).toISOString(),
    pid: 4242, args: {}, ...over,
  }
}

const PROPS = {
  search: '', onSearchChange: () => {}, activeScope: [],
  onScopeToggle: () => {}, active: true,
}

const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve() }

beforeEach(() => {
  historyTargets.mockReset().mockResolvedValue(['local'])
  getHistorySlim.mockReset().mockResolvedValue([rec()])
  getRunDetail.mockReset().mockResolvedValue({ logLines: [] })
  logsView.mockReset().mockResolvedValue({ ok: true, records: [] })
  getRunningRuns.mockReset().mockResolvedValue([])   // nothing running by default → no strip
  historySyncStatus.mockReset().mockResolvedValue({})  // every host read directly
})

afterEach(() => { vi.useRealTimers() })

describe('HistoryView', () => {
  it('renders rows after the first per-host fetch (default 7-day window)', async () => {
    render(<HistoryView {...PROPS} />)
    expect(await screen.findByText('backup')).toBeTruthy()
    expect(getHistorySlim).toHaveBeenCalledWith('local', 7)
    expect(getHistorySlim).toHaveBeenCalledTimes(1)
  })

  it('refetches with the selected window when the range changes', async () => {
    render(<HistoryView {...PROPS} />)
    await screen.findByText('backup')
    expect(getHistorySlim).toHaveBeenCalledWith('local', 7)
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByText('30d')) })
    await waitFor(() => expect(getHistorySlim).toHaveBeenCalledWith('local', 30))
  })

  it('renders each connected host incrementally and shows a per-host fetch strip', async () => {
    historyTargets.mockResolvedValue(['local', 'web-01'])
    getHistorySlim.mockImplementation((host: string) =>
      Promise.resolve([rec({ id: host, host, runnable: host === 'local' ? 'backup' : 'deploy' })]))
    render(<HistoryView {...PROPS} />)
    // Both hosts' runs merge into the table…
    expect(await screen.findByText('backup')).toBeTruthy()
    expect(await screen.findByText('deploy')).toBeTruthy()
    // …and the per-host fetch strip appears (only shown for >1 host).
    expect(screen.getByText('Per-host fetch:')).toBeTruthy()
  })

  it('a failing host does not block the others (isolated per-host reads)', async () => {
    historyTargets.mockResolvedValue(['local', 'web-01'])
    getHistorySlim.mockImplementation((host: string) =>
      host === 'local' ? Promise.resolve([rec()]) : Promise.reject(new Error('ssh timeout')))
    render(<HistoryView {...PROPS} />)
    // local's run still renders despite web-01 failing…
    expect(await screen.findByText('backup')).toBeTruthy()
    // …and the failed host is marked in the strip.
    expect(await screen.findByText('failed')).toBeTruthy()
  })

  it('refetches (once, debounced) on a runspec:run_end event', async () => {
    vi.useFakeTimers()
    render(<HistoryView {...PROPS} />)
    await act(async () => { await flush() })   // flush first (targets → slim) load
    expect(getHistorySlim).toHaveBeenCalledTimes(1)

    // A burst of run_end events collapses into a single debounced refetch.
    await act(async () => {
      window.dispatchEvent(new CustomEvent('runspec:run_end'))
      window.dispatchEvent(new CustomEvent('runspec:run_end'))
      await vi.advanceTimersByTimeAsync(800)
      await flush()
    })
    expect(getHistorySlim).toHaveBeenCalledTimes(2)
  })

  it('refetches only the named host on runspec:history_updated', async () => {
    historyTargets.mockResolvedValue(['local', 'web-01'])
    getHistorySlim.mockImplementation((host: string) =>
      Promise.resolve([rec({ id: host, host, runnable: host === 'local' ? 'backup' : 'deploy' })]))
    render(<HistoryView {...PROPS} />)
    await screen.findByText('deploy')
    getHistorySlim.mockClear()
    getHistorySlim.mockImplementation((host: string) =>
      Promise.resolve([rec({ id: 'new', host, runnable: 'rotate-certs' })]))
    await act(async () => {
      window.dispatchEvent(new CustomEvent('runspec:history_updated', { detail: { host: 'web-01' } }))
      await flush()
    })
    expect(getHistorySlim).toHaveBeenCalledTimes(1)
    expect(getHistorySlim).toHaveBeenCalledWith('web-01', 7)
    expect(await screen.findByText('rotate-certs')).toBeTruthy()
    expect(screen.getByText('backup')).toBeTruthy()  // local untouched
  })

  it('keeps a column filter selection across a refetch', async () => {
    getHistorySlim.mockResolvedValue([
      rec({ id: 'ok1', runId: 'ok1', runnable: 'backup', exitCode: 0 }),
      rec({ id: 'bad1', runId: 'bad1', runnable: 'deploy', exitCode: 2 }),
    ])
    const { container } = render(<HistoryView {...PROPS} />)
    await screen.findByText('deploy')
    // Status column → pick "error" → OK
    const triggers = container.querySelectorAll('.ant-table-filter-trigger')
    const statusTrigger = [...triggers].find(t => t.closest('th')?.textContent?.includes('Status')) as HTMLElement
    await act(async () => { statusTrigger.click() })
    const errorItem = (await screen.findAllByText('error')).find(el => el.closest('.ant-dropdown-menu-item'))!
    await act(async () => { (errorItem.closest('.ant-dropdown-menu-item') as HTMLElement).click() })
    await act(async () => { screen.getByRole('button', { name: 'OK' }).click() })
    await waitFor(() => expect(screen.queryByText('backup')).toBeNull())
    // A cache pull refetches the host (a re-render with fresh records)…
    await act(async () => {
      window.dispatchEvent(new CustomEvent('runspec:history_updated', { detail: { host: 'local' } }))
      await flush()
    })
    // …and the filter still applies.
    expect(screen.getByText('deploy')).toBeTruthy()
    expect(screen.queryByText('backup')).toBeNull()
  })

  it('a refetch while a filter dropdown is open keeps the unconfirmed ticks', async () => {
    getHistorySlim.mockResolvedValue([
      rec({ id: 'a', runId: 'a', operator: 'alice' }),
      rec({ id: 'b', runId: 'b', operator: 'bob', runnable: 'deploy' }),
    ])
    const { container, rerender } = render(<HistoryView {...PROPS} />)
    await screen.findByText('deploy')
    const trigger = [...container.querySelectorAll('.ant-table-filter-trigger')]
      .find(t => t.closest('th')?.textContent?.includes('Operator')) as HTMLElement
    const bob = () => screen.getAllByText('bob')
      .find(el => el.closest('.ant-dropdown-menu-item'))!.closest('.ant-dropdown-menu-item') as HTMLElement
    const ticked = () => bob().querySelector('.ant-checkbox-checked') !== null
    const refetch = async () => {
      window.dispatchEvent(new CustomEvent('runspec:history_updated', { detail: { host: 'local' } }))
      await flush()
    }
    // Commit "bob", reopen, untick it — then a cache pull refetches and the parent
    // re-renders (a fresh onScopeToggle) before OK is pressed.
    await act(async () => { trigger.click() })
    await act(async () => { bob().click() })
    await act(async () => { screen.getByRole('button', { name: 'OK' }).click() })
    await act(async () => { trigger.click() })
    await act(async () => { bob().click() })
    expect(ticked()).toBe(false)
    await act(refetch)
    rerender(<HistoryView {...PROPS} onScopeToggle={() => {}} />)
    expect(ticked()).toBe(false)
  })

  it('marks a cached host whose last sync failed "as of …"', async () => {
    historyTargets.mockResolvedValue(['local', 'web-01'])
    getHistorySlim.mockImplementation((host: string) => Promise.resolve([rec({ id: host, host })]))
    historySyncStatus.mockResolvedValue({
      'web-01': { cached: true, synced_at: new Date(Date.now() - 180_000).toISOString(), error: 'connection refused' },
    })
    render(<HistoryView {...PROPS} />)
    expect(await screen.findByText(/as of 3m ago/)).toBeTruthy()
  })

  it('does not poll within the (slowed) fallback window absent an event', async () => {
    vi.useFakeTimers()
    render(<HistoryView {...PROPS} />)
    await act(async () => { await flush() })
    expect(getHistorySlim).toHaveBeenCalledTimes(1)
    // Advance well past the old 8s cadence but under the new 25s poll — no refetch.
    await act(async () => { await vi.advanceTimersByTimeAsync(20000); await flush() })
    expect(getHistorySlim).toHaveBeenCalledTimes(1)
  })

  it('does not refetch when the search prop changes (client-side filter only)', async () => {
    const { rerender } = render(<HistoryView {...PROPS} />)
    await screen.findByText('backup')
    expect(getHistorySlim).toHaveBeenCalledTimes(1)
    rerender(<HistoryView {...PROPS} search="nomatch" />)
    await waitFor(() => expect(screen.queryByText('backup')).toBeNull())
    expect(getHistorySlim).toHaveBeenCalledTimes(1)   // no new fetch on filter
  })

  it('shows a Run by column distinguishing agent from operator runs', async () => {
    getHistorySlim.mockResolvedValue([
      rec({ id: 'r-user', runnable: 'backup', initiatedBy: 'user' }),
      rec({ id: 'r-agent', runnable: 'deploy', initiatedBy: 'llm' }),
    ])
    render(<HistoryView {...PROPS} />)
    await screen.findByText('backup')
    // Both the 'agent' and 'user' run-by labels render (the initiator indicator
    // is a dedicated column, not buried at the end of the ellipsized Runnable cell).
    expect(screen.getByText('agent')).toBeTruthy()
    expect(screen.getByText('user')).toBeTruthy()
  })

  it('fetches and shows the run log lines on demand when a row is expanded', async () => {
    getRunDetail.mockResolvedValue({
      logLines: [{ ts: '2026-06-03T14:22:05+00:00', level: 'INFO', message: 'fetched line' }],
    })
    render(<HistoryView {...PROPS} onRerun={() => {}} />)
    const user = userEvent.setup()
    const expandBtn = await screen.findByLabelText(/Expand row/i)
    await act(async () => { await user.click(expandBtn) })
    // Expanding fetches just this run's log (the bulk payload ships slim rows).
    await waitFor(() => expect(getRunDetail).toHaveBeenCalledWith('local', 'run-1'))
    expect(await screen.findByText('fetched line')).toBeTruthy()
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

  it('shows the Running-now strip for live runs (fleet-wide get_running_runs)', async () => {
    getRunningRuns.mockResolvedValue([running({ runnable: 'db-migrate' }), running({ id: 'live2', runId: 'live-2', runnable: 'backup', host: 'local' })])
    render(<HistoryView {...PROPS} />)
    // The strip header counts the live runs and each run's name renders in it.
    expect(await screen.findByText('2 running now')).toBeTruthy()
    expect(screen.getByText('db-migrate')).toBeTruthy()
    expect(getRunningRuns).toHaveBeenCalled()
  })

  it('hides the Running-now strip when nothing is running', async () => {
    getRunningRuns.mockResolvedValue([])
    render(<HistoryView {...PROPS} />)
    await screen.findByText('backup')          // history loaded
    expect(screen.queryByText(/running now/)).toBeNull()
  })

  it('shows a prune note (and skips the log fetch) for a pruned-log row', async () => {
    getHistorySlim.mockResolvedValue([
      rec({ runId: 'run-pruned', logPrunedAt: '2026-09-20T00:00:00+00:00', logPrunedBy: 'svc-runner' }),
    ])
    render(<HistoryView {...PROPS} onRerun={() => {}} />)
    const user = userEvent.setup()
    const expandBtn = await screen.findByLabelText(/Expand row/i)
    await act(async () => { await user.click(expandBtn) })
    // The expand panel says why the log is gone (by whom) instead of "no log lines"…
    const note = await screen.findByText(/log pruned by svc-runner/i)
    expect(note.textContent).toMatch(/record retained/i)
    // …and never fetches a log that isn't there.
    expect(getRunDetail).not.toHaveBeenCalled()
  })
})

describe('historyStatusMatches', () => {
  it('buckets skipped runs on their own', async () => {
    const { historyStatusMatches } = await import('./HistoryView')
    expect(historyStatusMatches({ exitCode: 0, status: 'skipped' }, 0)).toBe(false)
    expect(historyStatusMatches({ exitCode: 0, status: 'skipped' }, -1)).toBe(true)
    expect(historyStatusMatches({ exitCode: 0, status: 'completed' }, 0)).toBe(true)
    expect(historyStatusMatches({ exitCode: 2, status: 'failed' }, 1)).toBe(true)
    expect(historyStatusMatches({ exitCode: 0 }, -1)).toBe(false)
  })
})

describe('syncNote', () => {
  const now = Date.parse('2026-09-28T12:00:00Z')
  it('is null for a host read directly', () => {
    expect(syncNote(undefined, now)).toBeNull()
    expect(syncNote({ cached: false, synced_at: null, error: 'x' }, now)).toBeNull()
  })
  it('says "as of" only when the latest pull failed', () => {
    expect(syncNote({ cached: true, synced_at: '2026-09-28T11:58:00Z', error: '' }, now))
      .toEqual({ stale: false, note: '', tip: 'From the local history cache · synced 2m ago' })
    expect(syncNote({ cached: true, synced_at: '2026-09-28T09:00:00Z', error: 'timeout' }, now)?.note).toBe('as of 3h ago')
  })
  it('formats ages', () => {
    expect(agoLabel('2026-09-28T11:59:30Z', now)).toBe('30s ago')
    expect(agoLabel('2026-09-25T12:00:00Z', now)).toBe('3d ago')
  })
})

describe('accountNote', () => {
  it('shows the OS account only when it adds something', () => {
    expect(accountNote({ account: 'svc', operator: 'alice', runAs: '' })).toBe('svc')
    expect(accountNote({ account: 'alice', operator: 'alice', runAs: '' })).toBe('')
    expect(accountNote({ account: 'root', operator: 'alice', runAs: 'root' })).toBe('')
    expect(accountNote({ operator: 'alice', runAs: '' })).toBe('')
  })
})

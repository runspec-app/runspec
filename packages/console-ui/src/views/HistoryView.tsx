import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Table, Tag, Typography, Tooltip, message, Input, Drawer, Spin, Alert, Segmented, Switch } from 'antd'
import { RedoOutlined, CopyOutlined, RobotOutlined, UserOutlined, SearchOutlined, EditOutlined, ClockCircleOutlined, ProfileOutlined, PlayCircleOutlined } from '@ant-design/icons'
import { RUN_BY_FILTERS, isScheduled, runBy } from '../runBy'
import type { ColumnType } from 'antd/es/table'
import type { FilterValue } from 'antd/es/table/interface'
import { bridge, type HistoryRecord, type HistoryLogLine, type HistorySyncState, type LogViewResult, type RunningRun } from '../bridge'
import { LogStream } from '../components/LogStream'
import { MONO_FONT, RADIUS, statusText, textColor, useAccent, useIsDark, withAlpha } from '../ThemeContext'
import { EmptyState, IconAction, OnceHint, PageToolbar, RowActions, StatusTag, type RowAction, type StatusTone } from '../components/kit'

// History shows real runnable runs only, with who initiated each (operator vs the
// assistant). Whole agent turns and individual built-in tool calls are agent
// activity, not runs — the backend (get_history_all) returns only runnable runs
// and keeps them out of History; they remain in Analytics.

// A row's stable key. The record id is md5(run_id + runnable); History merges
// records across hosts, so prefix the host to rule out any cross-host id
// collision expanding the wrong row.
const rowKeyOf = (r: HistoryRecord): string => `${r.host}:${r.id}`

// Read window (days) — bounds the backend read so a first visit is quick. 7d is
// the default; 0 means "all". Mirrors the Analytics tab's range control.
const RANGE_OPTIONS = [
  { label: '7d', value: 7 },
  { label: '30d', value: 30 },
  { label: '90d', value: 90 },
  { label: 'All', value: 0 },
]

const { Title, Text } = Typography

// Per-host fetch state for the strip below the search box — makes a slow or
// offline host visible instead of hiding inside one fleet-wide spinner.
type HostFetch = { status: 'loading' | 'done' | 'error'; ms?: number; count?: number; error?: string }
// Cap the merged, sorted table so a large fleet (each host caps its own read)
// can't build an unbounded row list; the table paginates within this.
const MERGED_CAP = 500
const fmtMs = (ms?: number): string => (ms == null ? '—' : ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`)
const slowMs = (ms?: number): boolean => (ms ?? 0) >= 1500

/** "3m ago"-style age of an ISO timestamp. */
export function agoLabel(iso: string, now = Date.now()): string {
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return iso
  const s = Math.max(0, Math.round((now - t) / 1000))
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`
}

/** How a host's per-host chip describes its History-cache freshness, or null when
 *  the host is read directly. `stale` = showing cached runs because the latest
 *  pull failed (the chip reads "as of …"). */
export function syncNote(state: HistorySyncState | undefined, now = Date.now()): { stale: boolean; note: string; tip: string } | null {
  if (!state?.synced_at) return null
  const ago = agoLabel(state.synced_at, now)
  if (state.error) {
    return { stale: true, note: `as of ${ago}`, tip: `Showing cached history as of ${ago} — the last sync failed: ${state.error}` }
  }
  return { stale: false, note: '', tip: `From the local history cache · synced ${ago}` }
}

// Elapsed since an ISO start, ticked client-side by the Running-now strip so a
// live run's timer counts up without a re-fetch. "—" for an unparseable ts.
const fmtElapsed = (startTs: string, now: number): string => {
  const start = new Date(startTs).getTime()
  if (!start || Number.isNaN(start)) return '—'
  const s = Math.max(0, Math.floor((now - start) / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

const SOURCE_BADGE: Record<string, { label: string; color: string; title: string }> = {
  env:          { label: 'env',      color: 'blue',    title: 'Value came from an environment variable' },
  runspec_env:  { label: '.env',     color: 'purple',  title: 'Value came from the .runspec_env file' },
  spec_default: { label: 'default',  color: 'default', title: 'Value is the spec default — not explicitly provided' },
}

function ArgSourceBadge({ source }: { source: string | undefined }) {
  if (!source || source === 'cli' || source === 'not_set') return null
  const badge = SOURCE_BADGE[source]
  if (!badge) return null
  return (
    <Tooltip title={badge.title}>
      <Tag color={badge.color} style={{ fontSize: 11, margin: '0 0 0 2px', padding: '0 4px', lineHeight: '16px', cursor: 'default' }}>
        {badge.label}
      </Tag>
    </Tooltip>
  )
}

/** Text colour for a log line's level, readable in both themes. */
export function logLevelColour(level: string, isDark: boolean): string {
  const st = statusText(isDark)
  return level === 'ERROR' || level === 'CRITICAL' ? st.error
    : level === 'WARNING' ? st.warn
    : level === 'DEBUG' ? 'var(--rs-text-faint)'
    : 'var(--rs-text-secondary)'
}

function formatLogsAsText(record: HistoryRecord, logLines: HistoryLogLine[]): string {
  const header = `Log output from /${record.runnable} on ${record.host} (${new Date(record.ts).toLocaleString()})`
  const lines = logLines.map(l =>
    `${new Date(l.ts).toLocaleTimeString()} ${l.level.padEnd(8)} ${l.message}`
  )
  return [header, '', ...lines].join('\n')
}

interface ExpandedRunProps {
  record: HistoryRecord
  // Log lines for this run, fetched on demand when the row expands — the bulk
  // History payload omits them (see get_run_detail). `logsLoading` is true while
  // that fetch is in flight.
  logLines: HistoryLogLine[]
  logsLoading: boolean
  onRerun?: (record: HistoryRecord) => void
  onAskLlm?: (text: string) => void
  onViewFullLog?: (record: HistoryRecord) => void
}

function ExpandedRun({ record, logLines, logsLoading, onRerun, onAskLlm, onViewFullLog }: ExpandedRunProps) {
  const args = record.args ?? {}
  const hasArgs = Object.keys(args).length > 0

  // Edit-and-rerun opens the Forms run dialog pre-filled with this run's values
  // (rich typed inputs — selectors, switches, path pickers — and every available
  // arg, not just the ones recorded). App resolves the discovered runnable.
  const editInForm = () => window.dispatchEvent(new CustomEvent('runspec:edit_in_form', {
    detail: { host: record.host, runnable: record.runnable, args, commandPath: record.commandPath ?? [] },
  }))

  const handleCopy = () => {
    navigator.clipboard.writeText(formatLogsAsText(record, logLines))
    message.success('Copied to clipboard')
  }

  const handleAskLlm = () => {
    onAskLlm?.(formatLogsAsText(record, logLines))
  }

  // The inline lines above are the ones recorded for this run. The full merged
  // stream (every line, plus runs older than History's cap) lives in the venv
  // log — fetched on demand via logs_view, which needs the venv (group) and the
  // raw run_id. Hidden when either is unknown (e.g. a legacy no-run_id log, or a
  // multi-venv remote where the group couldn't be resolved).
  const canViewFullLog = !!(onViewFullLog && record.runId && record.group)

  // Rerun is the row's action; Edit & rerun and View full log sit beside it,
  // quieter. Edit & rerun is always offered, not gated on recorded args:
  // secrets (password-typed args) are never written to the audit record, so a
  // runnable whose required arg is a secret has empty `args` here and plain
  // Rerun would fail with "missing required argument". The form is the way to
  // supply it.
  const secondary: RowAction[] = []
  if (onRerun) secondary.push({
    key: 'edit', label: 'Edit & rerun', icon: <EditOutlined />, onClick: editInForm,
    tip: "Open the run dialog pre-filled with this run's values — rich typed inputs and every available arg, including required secrets that aren't recorded",
  })
  if (canViewFullLog) secondary.push({
    key: 'log', label: 'View full log', icon: <ProfileOutlined />, onClick: () => onViewFullLog!(record),
    tip: 'Open the complete merged log for this run — every line (the inline preview may be truncated), read from the venv log on demand',
  })

  return (
    <div style={{ padding: '12px 24px 12px 48px', display: 'flex', flexDirection: 'column', gap: 12 }}>
      {(onRerun || canViewFullLog) && (
        <RowActions
          primary={onRerun ? { key: 'rerun', label: 'Rerun', icon: <RedoOutlined />, onClick: () => onRerun(record) } : undefined}
          secondary={secondary}
        />
      )}

      {/* Args — static display (edit-and-rerun opens the Forms run dialog) */}
      <div>
        <Text type="secondary" style={{ fontSize: 12, textTransform: 'uppercase', letterSpacing: 1 }}>
          Arguments
        </Text>
        {hasArgs ? (
          <div style={{ marginTop: 6, display: 'flex', flexWrap: 'wrap', gap: '6px 16px', alignItems: 'center' }}>
            {Object.entries(args).map(([k, v]) => (
              <span key={k} style={{ display: 'inline-flex', alignItems: 'center' }}>
                <Text type="secondary" style={{ fontSize: 13 }}>--{k}=</Text>
                <Text code style={{ fontSize: 13 }}>{String(v)}</Text>
                <ArgSourceBadge source={record.argSources?.[k]} />
              </span>
            ))}
          </div>
        ) : (
          <Text type="secondary" style={{ fontSize: 13, marginLeft: 8 }}>none</Text>
        )}
      </div>

      {/* Log lines */}
      <div>
        <Text type="secondary" style={{ fontSize: 12, textTransform: 'uppercase', letterSpacing: 1 }}>
          Log
        </Text>
        <div style={{
          position: 'relative',
          marginTop: 6,
          background: 'var(--rs-surface-sunken)', border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm,
          padding: '8px 12px', fontFamily: MONO_FONT, fontSize: 13, lineHeight: 1.8,
        }}>
          {logLines.length > 0 && (
            <div style={{ position: 'absolute', top: 5, right: 6, display: 'flex', gap: 2 }}>
              <IconAction label="Copy log" tip="Copy the log to the clipboard" icon={<CopyOutlined />} onClick={handleCopy} />
              {onAskLlm && (
                <IconAction label="Ask the assistant about this log" icon={<RobotOutlined />} onClick={handleAskLlm} />
              )}
            </div>
          )}
          {record.logPrunedAt
            ? <span style={{ color: 'var(--rs-text-muted)' }}>
                log pruned by {record.logPrunedBy || 'unknown'} on {new Date(record.logPrunedAt).toLocaleDateString()} — record retained
              </span>
            : logsLoading
            ? <Spin size="small" />
            : logLines.length === 0
            ? <span style={{ color: 'var(--rs-text-muted)' }}>no log lines</span>
            : logLines.map((line, i) => <LogLine key={i} line={line} />)
          }
        </div>
      </div>
    </div>
  )
}

function LogLine({ line }: { line: HistoryLogLine }) {
  const isDark = useIsDark()
  const time = new Date(line.ts).toLocaleTimeString()
  return (
    <div>
      <span style={{ color: 'var(--rs-text-muted)', marginRight: 10 }}>{time}</span>
      <span style={{ color: logLevelColour(line.level, isDark), marginRight: 10, minWidth: 56, display: 'inline-block' }}>{line.level}</span>
      <span style={{ color: textColor(isDark) }}>{line.message}</span>
    </div>
  )
}

// Lazy per-run drill: opens the complete merged stream for one run via
// logs_view (the same read the Logs tab's "View merged stream" uses, scoped to
// a single run_id). Fetched only when a run is opened — never on every row
// expand — so History's cheap inline preview stays the default.
function RunLogDrawer({ record, onClose }: { record: HistoryRecord | null; onClose: () => void }) {
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<LogViewResult | null>(null)

  useEffect(() => {
    if (!record) { setResult(null); return }
    let cancelled = false
    setLoading(true)
    bridge.logs_view(record.host, record.group, record.runnable, undefined, undefined, record.runId)
      .then(r => { if (!cancelled) setResult(r) })
      .catch(e => { if (!cancelled) message.error(String(e)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [record])

  return (
    <Drawer
      open={!!record}
      onClose={onClose}
      // Match the Logs tab's merged-stream drawer: a wide, responsive panel so a
      // full log isn't crammed into a narrow 680px column on a large window.
      width={Math.min(1400, Math.max(680, window.innerWidth - 120))}
      title={record ? <span style={{ fontFamily: MONO_FONT }}>/{record.runnable} — full log ({(record.runId ?? '').slice(0, 8)})</span> : ''}
    >
      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}><Spin /></div>
      ) : !result?.ok ? (
        <Alert type="error" showIcon message={result?.error || 'Failed to read logs'} />
      ) : result.records.length === 0 ? (
        <EmptyState message="No log records for this run." />
      ) : (
        <LogStream records={result.records} />
      )}
    </Drawer>
  )
}

// Live "Running now" strip pinned above the History table — the in-flight runs
// the per-venv runs index holds across the fleet (get_running_runs), whoever
// launched them. Rendered only when something is running (the parent gates on
// rows.length), and it owns a 1 s tick so each run's elapsed time counts up
// without a re-fetch; the row set itself refreshes on History's 25 s poll and the
// debounced run_end. A finished/crashed run drops off (the backend liveness-
// filters and only reports genuinely-live rows).
function RunningNowStrip({ rows, accent }: { rows: RunningRun[]; accent: string }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])
  return (
    <div
      style={{
        marginBottom: 12,
        padding: '8px 12px',
        background: withAlpha(accent, 0.06),
        border: `1px solid ${withAlpha(accent, 0.35)}`,
        borderRadius: RADIUS.sm,
        display: 'flex',
        flexWrap: 'wrap',
        gap: 8,
        alignItems: 'center',
      }}
    >
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, color: accent, fontSize: 12, fontWeight: 600 }}>
        <span className="rs-pulse" style={{ width: 7, height: 7, borderRadius: '50%', background: accent, display: 'inline-block' }} />
        {rows.length} running now
      </span>
      {rows.map(r => {
        const argStr = Object.entries(r.args ?? {}).map(([k, v]) => `--${k}=${String(v)}`).join(' ')
        const tip = [
          `${r.group ? r.group + '/' : ''}${r.runnable} on ${r.host}`,
          `started ${new Date(r.startTs).toLocaleString()}`,
          r.operator ? `by ${r.operator}` : '',
          r.runAs ? `as ${r.runAs}` : '',
          r.account && r.account !== r.operator && r.account !== r.runAs ? `OS account ${r.account}` : '',
          r.pending ? 'in the logs inbox (another account\'s run)' : '',
          r.pid ? `pid ${r.pid}` : '',
          argStr,
        ].filter(Boolean).join('\n')
        return (
          <Tooltip key={`${r.host}:${r.id}`} title={<span style={{ whiteSpace: 'pre-line' }}>{tip}</span>}>
            <span
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                fontSize: 12,
                fontFamily: MONO_FONT,
                color: 'var(--rs-text-secondary)',
                border: '1px solid var(--rs-border)',
                borderRadius: 4,
                padding: '1px 8px',
                whiteSpace: 'nowrap',
              }}
            >
              {isScheduled(r.trigger) && <ClockCircleOutlined style={{ color: 'var(--rs-text-muted)', fontSize: 12 }} />}
              {runBy(r).kind === 'agent'
                ? <RobotOutlined style={{ color: accent, fontSize: 12 }} />
                : <UserOutlined style={{ color: 'var(--rs-text-muted)', fontSize: 12 }} />}
              <span>
                {r.group && <span style={{ color: 'var(--rs-text-muted)' }}>{r.group}/</span>}
                {r.runnable}
              </span>
              <Tag style={{ margin: 0, fontSize: 11, lineHeight: '16px', padding: '0 5px' }}>{r.host}</Tag>
              <span style={{ color: accent }}>{fmtElapsed(r.startTs, now)}</span>
            </span>
          </Tooltip>
        )
      })}
    </div>
  )
}

interface HistoryViewProps {
  search: string
  onSearchChange: (s: string) => void
  onRerun?: (record: HistoryRecord) => void
  onAskLlm?: (text: string) => void
  activeScope: string[]
  onScopeToggle: (group: string) => void
  active: boolean
}

// Remembers the "Show hidden" switch across sessions (a per-viewer convenience).
const SHOW_HIDDEN_KEY = 'rs.history.showHidden'

function loadShowHidden(): boolean {
  try { return localStorage.getItem(SHOW_HIDDEN_KEY) === '1' } catch { return false }
}

function saveShowHidden(on: boolean): void {
  try { localStorage.setItem(SHOW_HIDDEN_KEY, on ? '1' : '0') } catch { /* storage blocked */ }
}

/** What the "Hiding" indicator says for the runnables History leaves out
 *  (per host, from history_hidden_runnables): the names across hosts, each with
 *  the hosts it's hidden on, and a short label — the name when one, else a count.
 *  `null` when nothing is hidden on the hosts read. */
export function hiddenSummary(byHost: Record<string, string[]>, hosts: string[]): { label: string; names: { name: string; hosts: string[] }[] } | null {
  const where = new Map<string, string[]>()
  for (const host of hosts) {
    for (const name of byHost[host] ?? []) where.set(name, [...(where.get(name) ?? []), host])
  }
  if (where.size === 0) return null
  const names = [...where.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([name, h]) => ({ name, hosts: h }))
  return { label: names.length === 1 ? `Hiding: ${names[0].name}` : `Hiding ${names.length} runnables`, names }
}

/** The History search box: words starting with `-` leave out the runs whose
 *  runnable name contains them (`-heartbeat -sync`); the rest is matched as one
 *  substring across runnable, venv, host, operator, run-as, label and command,
 *  as before. A lone `-` is plain text. */
export function parseHistorySearch(search: string): { text: string; exclude: string[] } {
  const exclude: string[] = []
  const rest: string[] = []
  for (const word of search.trim().split(/\s+/)) {
    if (word.length > 1 && word.startsWith('-')) exclude.push(word.slice(1).toLowerCase())
    else if (word) rest.push(word)
  }
  return { text: rest.join(' ').toLowerCase(), exclude }
}

/** Status-column filter: 0 = ok, 1 = error, -1 = skipped (a `runspec exec` run
 *  that never started — exit 0 but not a success). */
export function historyStatusMatches(record: Pick<HistoryRecord, 'exitCode' | 'status'>, value: number): boolean {
  if (record.status === 'skipped') return value === -1
  if (value === -1) return false
  return value === 0 ? record.exitCode === 0 : record.exitCode !== 0
}

export function HistoryView({ search, onSearchChange, onRerun, onAskLlm, activeScope, onScopeToggle, active }: HistoryViewProps) {
  // History reads one host at a time (local + every connected remote) and merges
  // the results, so each host renders as it arrives. recordsByHost holds each
  // host's slim rows; hostStatus/targets drive the per-host fetch strip.
  const [recordsByHost, setRecordsByHost] = useState<Record<string, HistoryRecord[]>>({})
  const [hostStatus, setHostStatus] = useState<Record<string, HostFetch>>({})
  const [targets, setTargets] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  // History spans the whole fleet — the local host plus every connected remote —
  // so automated runs (scheduled/trigger runnable runs on their own targets, and
  // agent/trigger/schedule turns logged on the console host) surface regardless
  // of the sidebar selection. The Host column filter narrows within the set.
  // Every column filter's selection, keyed by column key — fully controlled so a
  // re-render (a poll / run_end / history_updated refetch) never clears it. The
  // columns used to pin `filteredValue: []`, which snapped any selection back to
  // empty on the next render.
  const [colFilters, setColFilters] = useState<Record<string, FilterValue | null>>({})
  const [expandedKeys, setExpandedKeys] = useState<string[]>([])
  const [fullLogRecord, setFullLogRecord] = useState<HistoryRecord | null>(null)
  // Read window in days (0 = all) — bounds the backend read so a first visit is
  // quick. Changing it refetches (see the effect's deps).
  const [sinceDays, setSinceDays] = useState(7)
  // Runs of runnables declared `discoverable = false` (internal helpers, e.g. a
  // job cron runs every minute) are left out server-side unless this is on.
  const [showHidden, setShowHidden] = useState(loadShowHidden)
  // Per host, what that switch is hiding — so the tab never hides silently.
  const [hiddenByHost, setHiddenByHost] = useState<Record<string, string[]>>({})
  const hidden = useMemo(() => hiddenSummary(hiddenByHost, targets), [hiddenByHost, targets])
  // Per-run log lines fetched lazily when a row expands (the bulk payload ships
  // slim rows), keyed by rowKeyOf(r); detailLoading tracks the in-flight fetch.
  const [details, setDetails] = useState<Record<string, HistoryLogLine[]>>({})
  const [detailLoading, setDetailLoading] = useState<Record<string, boolean>>({})
  // Live "what's running" across the fleet (get_running_runs) — the strip above
  // the table. Refreshed on the same cadence as the history read below (initial +
  // 25 s poll + debounced run_end), so it costs no extra timer; the strip itself
  // ticks the per-run elapsed time.
  const [running, setRunning] = useState<RunningRun[]>([])
  // Per remote host: History-cache freshness (history_sync_status) — the chip
  // says "as of …" when a cached host's latest pull failed.
  const [sync, setSync] = useState<Record<string, HistorySyncState>>({})
  const accent = useAccent()

  // The view stays mounted across tab switches (App toggles display), so records
  // persist — returning shows the last data instantly. Reads only happen while
  // visible. History reads the local host plus every *connected* remote, but
  // **one host at a time** (history_targets → a get_history_slim per host),
  // rendering each host's runs the moment they arrive: a slow or offline host
  // adds its own latency, never the whole view's (the old single get_history_all
  // joined on the slowest host before showing anything). Each host's fetch time
  // is surfaced in the per-host strip below the search box. Automated runs
  // (scheduled/trigger runs on their own targets, agent/trigger/schedule turns on
  // the console host) still surface regardless of the sidebar selection; the Host
  // column filter narrows within the merged set. Primary refresh is event-driven
  // (run_end, debounced); a 25 s poll catches anything the event path misses.
  useEffect(() => {
    if (!active) return
    let cancelled = false
    let debounce: ReturnType<typeof setTimeout> | undefined

    // Fleet-wide live running rows — one call (not per-host), tolerant of the
    // mock/proxy returning a non-array. Shares runLoad's cadence.
    const loadRunning = () => {
      Promise.resolve(bridge.get_running_runs(showHidden))
        .then(rows => { if (!cancelled) setRunning(Array.isArray(rows) ? rows : []) })
        .catch(() => { if (!cancelled) setRunning([]) })
    }

    const loadSync = () => {
      Promise.resolve(bridge.history_sync_status())
        .then(s => { if (!cancelled) setSync(s && typeof s === 'object' ? s : {}) })
        .catch(() => {})
    }

    // One host's rows (a remote on the History cache returns instantly).
    const fetchHost = (name: string) => {
      const t0 = performance.now()
      return bridge.get_history_slim(name, sinceDays, showHidden)
        .then(rows => {
          if (cancelled) return
          setRecordsByHost(prev => ({ ...prev, [name]: rows }))
          setHostStatus(prev => ({ ...prev, [name]: { status: 'done', ms: Math.round(performance.now() - t0), count: rows.length } }))
        })
        .catch(e => {
          if (cancelled) return
          setHostStatus(prev => ({ ...prev, [name]: { status: 'error', ms: Math.round(performance.now() - t0), error: String(e) } }))
        })
    }

    const runLoad = async (showSpinner: boolean) => {
      loadRunning()
      if (showSpinner) { setLoading(true); setRecordsByHost({}) }
      let hosts: string[]
      try { hosts = await bridge.history_targets() } catch { hosts = ['local'] }
      if (cancelled) return
      setTargets(hosts)
      Promise.resolve(bridge.history_hidden_runnables(hosts))
        .then(h => { if (!cancelled) setHiddenByHost(h && typeof h === 'object' ? h : {}) })
        .catch(() => {})
      if (showSpinner) {
        setHostStatus(Object.fromEntries(hosts.map(h => [h, { status: 'loading' as const }])))
      }
      // Drop any host that's no longer a target (disconnected since last load) so
      // its stale rows don't linger in the merge.
      setRecordsByHost(prev => {
        const next: Record<string, HistoryRecord[]> = {}
        for (const h of hosts) if (prev[h]) next[h] = prev[h]
        return next
      })
      if (hosts.length === 0) { setLoading(false); return }
      let pending = hosts.length
      for (const name of hosts) {
        fetchHost(name)
          .finally(() => { if (!cancelled) { pending -= 1; if (pending === 0) { setLoading(false); loadSync() } } })
      }
    }

    runLoad(true)
    const id = setInterval(() => runLoad(false), 25000)

    // A run just ended → its run_summary is now on disk. Debounce so a burst of
    // events (a group fan-out) triggers a single refetch. Never shows a spinner.
    const onRunEnd = () => {
      if (debounce) clearTimeout(debounce)
      debounce = setTimeout(() => { if (!cancelled) runLoad(false) }, 750)
    }
    window.addEventListener('runspec:run_end', onRunEnd)

    // The History cache pulled a host and something changed → refetch just that
    // host (from the cache — no SSH on the tab's path).
    const onHistoryUpdated = (e: Event) => {
      const host = (e as CustomEvent<{ host?: string }>).detail?.host
      if (!host || cancelled) return
      fetchHost(host).then(loadSync)
    }
    window.addEventListener('runspec:history_updated', onHistoryUpdated)

    return () => {
      cancelled = true
      clearInterval(id)
      if (debounce) clearTimeout(debounce)
      window.removeEventListener('runspec:run_end', onRunEnd)
      window.removeEventListener('runspec:history_updated', onHistoryUpdated)
    }
  }, [active, sinceDays, showHidden])

  // Merge every host's slim rows into one newest-first list (capped so a large
  // fleet can't build an unbounded table). Recomputes as each host resolves.
  const records = useMemo<HistoryRecord[]>(() => {
    const all: HistoryRecord[] = []
    for (const rows of Object.values(recordsByHost)) all.push(...rows)
    all.sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0))
    return all.length > MERGED_CAP ? all.slice(0, MERGED_CAP) : all
  }, [recordsByHost])

  // Fetch a run's log lines the first time its row is expanded — the bulk payload
  // omits them (get_run_detail). Cached by rowKeyOf so a re-expand never refetches;
  // a run with no run_id (legacy log) has no fetchable detail.
  const fetchDetail = (record: HistoryRecord) => {
    const key = rowKeyOf(record)
    // A pruned-log row has no file to read — the note is driven by logPrunedAt on
    // the record itself, so skip the (pointless) get_run_detail fetch.
    if (!record.runId || record.logPrunedAt || details[key] !== undefined || detailLoading[key]) return
    setDetailLoading(d => ({ ...d, [key]: true }))
    // logFile (from the runs index) lets the backend read just this run's file.
    bridge.get_run_detail(record.host, record.runId, record.group, record.logFile)
      .then(res => setDetails(d => ({ ...d, [key]: res.logLines ?? [] })))
      .catch(() => setDetails(d => ({ ...d, [key]: [] })))
      .finally(() => setDetailLoading(d => { const n = { ...d }; delete n[key]; return n }))
  }

  const filtered = useMemo(() => {
    const { text: q, exclude } = parseHistorySearch(search)
    if (!q && exclude.length === 0) return records
    return records.filter(r =>
      !exclude.some(x => r.runnable.toLowerCase().includes(x)) && (
      !q ||
      r.runnable.toLowerCase().includes(q) ||
      r.group.toLowerCase().includes(q) ||
      r.host.toLowerCase().includes(q) ||
      r.operator.toLowerCase().includes(q) ||
      r.runAs.toLowerCase().includes(q) ||
      (r.label ?? '').toLowerCase().includes(q) ||
      (r.integrity ?? '').toLowerCase().includes(q) ||
      (r.commandPath ?? []).join(' ').toLowerCase().includes(q))
    )
  }, [records, search])

  // The filter options, keyed by their *content*: a refetch that returns the same
  // hosts/groups/operators keeps the same arrays, so the columns below keep their
  // identity. Ant re-syncs an open filter dropdown's ticks to the committed value
  // whenever the columns are rebuilt, so rebuilding them on every refetch (a cache
  // pull's history_updated arrives every few seconds on 7d/30d) undid a tick or
  // untick before the operator could press OK.
  const facetKey = useMemo(() => JSON.stringify([
    [...new Set(records.map(r => r.runnable))].sort(),
    [...new Set(records.map(r => r.host))],
    [...new Set(records.map(r => r.group))],
    [...new Set(records.map(r => r.operator))],
    [...new Set(records.map(r => r.runAs))],
    [...new Set(records.map(r => r.runbook).filter((s): s is string => !!s))],
    [...new Set(records.map(r => r.label).filter((s): s is string => !!s))],
  ]), [records])
  const facets = useMemo(() => {
    const [runnableNames, hosts, groups, operators, runAsUsers, runbooks, labels] = JSON.parse(facetKey) as string[][]
    return { runnableNames, hosts, groups, operators, runAsUsers, runbooks, labels }
  }, [facetKey])
  const { runnableNames, hosts, groups, operators, runAsUsers, runbooks, labels } = facets
  // App passes a fresh onScopeToggle on every render; read it through a ref so an
  // unrelated App re-render doesn't rebuild the columns (same reason as above).
  const scopeToggleRef = useRef(onScopeToggle)
  useLayoutEffect(() => { scopeToggleRef.current = onScopeToggle })

  const columns: ColumnType<HistoryRecord>[] = useMemo(() => [
    {
      title: 'Runnable',
      key: 'runnable',
      // Bounded width + ellipsis so a wide window doesn't dump all the slack into
      // this (the only unsized) column, cramping the fixed-content ones. The
      // status indicators are front-loaded (before the ellipsis) so they're never
      // truncated, and the name carries a hover tooltip with its full text — the
      // ellipsis alone hid whether a long-named run was on a schedule/agent.
      width: 280,
      ellipsis: true,
      render: (_: unknown, r: HistoryRecord) => {
        const cmd = (r.commandPath ?? []).join(' ')
        const full = `${r.group ? r.group + '/' : ''}${r.runnable}${cmd ? ' ' + cmd : ''}`
        return (
          <span style={{ fontFamily: MONO_FONT, fontSize: 13, display: 'inline-flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
            {isScheduled(r.trigger) && (
              <Tooltip title="Ran on a schedule">
                <ClockCircleOutlined style={{ color: 'var(--rs-text-muted)', fontSize: 13, flex: '0 0 auto' }} />
              </Tooltip>
            )}
            <Tooltip title={full}>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {r.group && <span style={{ color: 'var(--rs-text-muted)' }}>{r.group}/</span>}{r.runnable}
                {cmd && <span style={{ color: 'var(--rs-text-muted)' }}> {cmd}</span>}
              </span>
            </Tooltip>
            <IntegrityTag record={r} />
          </span>
        )
      },
      sorter: (a, b) => `${a.group}/${a.runnable}`.localeCompare(`${b.group}/${b.runnable}`),
      filters: runnableNames.map(n => ({ text: n, value: n })),
      filterSearch: true,
      onFilter: (value, record) => record.runnable === value,
      filteredValue: colFilters['runnable'] ?? null,
    },
    {
      // Dedicated column so "run by agent vs operator" is always visible — the
      // old inline robot icon lived at the end of the ellipsized Runnable cell
      // and was the first thing truncated on a narrow window (the reported
      // "can't see if run by agent or user").
      title: 'Run by',
      key: 'runBy',
      width: 110,
      render: (_: unknown, r: HistoryRecord) => {
        const by = runBy(r)
        const icon = by.kind === 'agent' ? <RobotOutlined style={{ fontSize: 13 }} />
          : by.kind === 'user' ? <UserOutlined style={{ fontSize: 13 }} />
          : by.kind === 'run-now' ? <PlayCircleOutlined style={{ fontSize: 13 }} />
          : <ClockCircleOutlined style={{ fontSize: 13 }} />
        return (
          <Tooltip title={by.tip}>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color: by.kind === 'agent' ? accent : 'var(--rs-text-muted)', whiteSpace: 'nowrap' }}>
              {icon}{by.label}
            </span>
          </Tooltip>
        )
      },
      filters: RUN_BY_FILTERS,
      onFilter: (value, record) => runBy(record).kind === value,
      filteredValue: colFilters['runBy'] ?? null,
    },
    {
      title: 'Group',
      dataIndex: 'group',
      key: 'group',
      render: (g: string) => {
        const active = activeScope.includes(g)
        return (
          <Tag
            color={active ? 'geekblue' : 'blue'}
            onClick={() => scopeToggleRef.current(g)}
            style={{ cursor: 'pointer', fontWeight: active ? 600 : 400 }}
            title={active ? 'Remove from scope' : 'Add to scope'}
          >
            {g}
          </Tag>
        )
      },
      filters: groups.map(g => ({ text: g, value: g })),
      onFilter: (value, record) => record.group === value,
      filteredValue: colFilters['group'] ?? null,
    },
    ...(runbooks.length > 0 ? [{
      title: 'Runbook',
      key: 'runbook',
      render: (_: unknown, r: HistoryRecord) => r.runbook
        ? <Tag color="purple" style={{ fontFamily: MONO_FONT }}>${r.runbook}</Tag>
        : <span style={{ color: 'var(--rs-text-faint)' }}>—</span>,
      filters: runbooks.map(s => ({ text: `$${s}`, value: s })),
      onFilter: (value, record) => record.runbook === value,
      filteredValue: colFilters['runbook'] ?? null,
    } as ColumnType<HistoryRecord>] : []),
    // The `runspec exec --label` tag (e.g. "tui" for runspec-tui runs, "sched:<id>"
    // for a shared cron schedule) — shown only once some run in view carries one.
    ...(labels.length > 0 ? [{
      title: 'Label',
      key: 'label',
      render: (_: unknown, r: HistoryRecord) => r.label
        ? <Tag style={{ fontFamily: MONO_FONT }}>{r.label}</Tag>
        : <span style={{ color: 'var(--rs-text-faint)' }}>—</span>,
      filters: labels.map(s => ({ text: s, value: s })),
      onFilter: (value, record) => record.label === value,
      filteredValue: colFilters['label'] ?? null,
    } as ColumnType<HistoryRecord>] : []),
    {
      title: 'Host',
      dataIndex: 'host',
      key: 'host',
      render: (h: string) => <Tag>{h}</Tag>,
      filters: hosts.map(h => ({ text: h, value: h })),
      onFilter: (value, record) => record.host === value,
      filteredValue: colFilters['host'] ?? null,
    },
    {
      title: 'Operator',
      dataIndex: 'operator',
      key: 'operator',
      filters: operators.map(u => ({ text: u, value: u })),
      onFilter: (value, record) => record.operator === value,
      filteredValue: colFilters['operator'] ?? null,
      // The self-reported operator, plus the kernel-attested account when they
      // differ (runspec >= 0.66) — e.g. a sudo'd run, or a claim that doesn't match.
      render: (op: string, record: HistoryRecord) => {
        const acct = accountNote(record)
        return (
          <span>
            {op}
            {acct && (
              <Tooltip title={`Ran as OS account ${acct} (from the file / process owner — trustworthy; the operator name is self-reported)`}>
                <Tag style={{ marginLeft: 6, fontSize: 11, fontFamily: MONO_FONT }}>{acct}</Tag>
              </Tooltip>
            )}
          </span>
        )
      },
    },
    {
      title: 'Run as',
      dataIndex: 'runAs',
      key: 'runAs',
      render: (u: string) => <code style={{ fontSize: 13 }}>{u}</code>,
      filters: runAsUsers.map(u => ({ text: u, value: u })),
      onFilter: (value, record) => record.runAs === value,
      filteredValue: colFilters['runAs'] ?? null,
    },
    {
      title: 'Status',
      dataIndex: 'exitCode',
      key: 'exitCode',
      // A `runspec exec` skip (lock held / already running) has exit code 0 but
      // never ran — its own tag, and excluded from both ok and error.
      render: (c: number, record: HistoryRecord) => (
        <span style={{ display: 'inline-flex', gap: 4 }}>
          {record.status === 'skipped'
            ? <StatusTag tone="neutral" tip={`Didn't start — the previous run was still going${record.label ? ` (${record.label})` : ''}`}>skipped</StatusTag>
            : record.status === 'not_started'
              ? <StatusTag tone="failed" tip={`The runnable never started${record.reason ? `: ${record.reason}` : ''}. Recorded by this console, since the host has no record of it.`}>didn't start</StatusTag>
              : <StatusTag tone={c === 0 ? 'ok' : 'failed'}>{c === 0 ? 'ok' : `exit ${c}`}</StatusTag>}
          {record.pending && (
            <StatusTag tone="info" tip="Another account's run, still in the venv's logs inbox — the owner's next run (or Sweep inbox under Logs → Runs index) indexes it">inbox</StatusTag>
          )}
        </span>
      ),
      filters: [
        { text: 'ok', value: 0 },
        { text: 'error', value: 1 },
        { text: 'skipped', value: -1 },
      ],
      onFilter: (value, record) => historyStatusMatches(record, value as number),
      filteredValue: colFilters['exitCode'] ?? null,
    },
    {
      title: 'Duration',
      dataIndex: 'durationMs',
      key: 'durationMs',
      render: (ms: number) => `${(ms / 1000).toFixed(2)}s`,
      sorter: (a, b) => a.durationMs - b.durationMs,
    },
    {
      title: 'Time',
      dataIndex: 'ts',
      key: 'ts',
      render: (ts: string) => new Date(ts).toLocaleString(),
      sorter: (a, b) => new Date(a.ts).getTime() - new Date(b.ts).getTime(),
      defaultSortOrder: 'descend',
    },
  ], [runnableNames, groups, runbooks, labels, hosts, operators, runAsUsers, colFilters, activeScope, accent])

  return (
    <div>
      <OnceHint id="history.rows">
        Click a run to see its arguments and log, and to rerun it. In the search, <span style={{ fontFamily: MONO_FONT }}>-name</span> leaves a runnable out.
      </OnceHint>
      <PageToolbar>
        <Input
          prefix={<SearchOutlined />}
          placeholder="Search runnable, host, operator… (-name excludes)"
          value={search}
          onChange={e => onSearchChange(e.target.value)}
          allowClear
          style={{ maxWidth: 400 }}
        />
        <Tooltip title="How far back to load. A shorter window loads faster; widen it to see older runs.">
          <Segmented
            size="small"
            value={sinceDays}
            onChange={v => setSinceDays(v as number)}
            options={RANGE_OPTIONS}
          />
        </Tooltip>
        <Tooltip title="Also show runs of runnables marked discoverable = false in their runspec.toml (internal helpers, such as a job cron runs every minute). They're left out by default.">
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap' }}>
            <Switch
              size="small"
              checked={showHidden}
              onChange={on => { setShowHidden(on); saveShowHidden(on) }}
              aria-label="Show hidden runnables"
            />
            <Text type="secondary" style={{ fontSize: 12 }}>Show hidden</Text>
          </span>
        </Tooltip>
        {hidden && !showHidden && (
          <Tooltip
            title={
              <div>
                <div style={{ marginBottom: 4 }}>Runs of these runnables (discoverable = false) are left out. Click to show them.</div>
                {hidden.names.map(n => (
                  <div key={n.name}><span style={{ fontFamily: MONO_FONT }}>{n.name}</span> on {n.hosts.join(', ')}</div>
                ))}
              </div>
            }
          >
            <Tag
              data-testid="history-hiding"
              style={{ cursor: 'pointer', margin: 0, fontSize: 12 }}
              onClick={() => { setShowHidden(true); saveShowHidden(true) }}
            >
              {hidden.label}
            </Tag>
          </Tooltip>
        )}
      </PageToolbar>
      {/* Live "Running now" strip — in-flight runs from the runs index (see
          RunningNowStrip). Shown only when something is running so it stays
          unobtrusive; pinned above the finished-runs table. */}
      {running.length > 0 && <RunningNowStrip rows={running} accent={accent} />}
      {/* Hosts read: one quiet line when every host answered, and a chip only
          for a host that is still loading, slow, failed or showing a stale
          cache, so an offline host is visible without a debug strip on every
          visit. Only shown when reading more than the local host. */}
      {targets.length > 1 && (() => {
        const chips = targets.map(name => {
          const s = hostStatus[name]
          const st = s?.status ?? 'loading'
          const cache = st === 'done' ? syncNote(sync[name]) : null
          const tone: StatusTone = st === 'error' ? 'failed'
            : st === 'loading' ? 'neutral'
            : cache?.stale || slowMs(s?.ms) ? 'attention' : 'ok'
          const detail = st === 'loading' ? 'loading…' : st === 'error' ? 'failed'
            : cache?.stale ? cache.note : `slow · ${fmtMs(s?.ms)}`
          const tip = st === 'error' ? (s?.error || 'fetch failed')
            : st === 'loading' ? 'fetching…'
            : cache ? `${s?.count ?? 0} run(s) — ${cache.tip}`
            : `${s?.count ?? 0} run(s) in ${fmtMs(s?.ms)}${slowMs(s?.ms) ? ' — slow' : ''}`
          return { name, tone, detail, tip, count: s?.count ?? 0 }
        })
        const issues = chips.filter(c => c.tone !== 'ok')
        const allTip = chips.map(c => `${c.name}: ${c.tip}`).join('\n')
        return (
          <div data-testid="history-hosts" style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 12, alignItems: 'center' }}>
            <Tooltip title={<span style={{ whiteSpace: 'pre-line' }}>{allTip}</span>}>
              <Text type="secondary" style={{ fontSize: 12, cursor: 'default' }}>
                {targets.length} hosts{issues.length === 0 ? ' · all loaded' : ''}
              </Text>
            </Tooltip>
            {issues.map(c => (
              <StatusTag key={c.name} tone={c.tone} tip={c.tip} mono>
                {c.name} · {c.detail}
              </StatusTag>
            ))}
          </div>
        )
      })()}
      <Table
        dataSource={filtered}
        columns={columns}
        rowKey={rowKeyOf}
        size="small"
        loading={loading && records.length === 0}
        // Narrow windows scroll sideways instead of squeezing columns to one
        // character each.
        scroll={{ x: 'max-content' }}
        locale={{
          emptyText: loading ? ' ' : (
            <EmptyState
              message={search ? 'No runs match the search.'
                : sinceDays ? `No runs in the last ${sinceDays} days.` : 'No runs recorded yet.'}
              action={search ? { label: 'Clear search', onClick: () => onSearchChange('') }
                : sinceDays && sinceDays < 90 ? { label: `Show ${sinceDays === 7 ? 30 : 90} days`, onClick: () => setSinceDays(sinceDays === 7 ? 30 : 90) }
                : undefined}
            />
          ),
        }}
        pagination={{ pageSize: 50, showSizeChanger: true }}
        onChange={(_, filters) => setColFilters(filters)}
        expandable={{
          expandedRowRender: (record) => {
            const key = rowKeyOf(record)
            return (
              <ExpandedRun
                record={record}
                logLines={details[key] ?? []}
                logsLoading={!!detailLoading[key]}
                onRerun={onRerun}
                onAskLlm={onAskLlm}
                onViewFullLog={setFullLogRecord}
              />
            )
          },
          rowExpandable: () => true,
          expandedRowKeys: expandedKeys,
          onExpand: (expanded, record) => { if (expanded) fetchDetail(record) },
          onExpandedRowsChange: (keys) => setExpandedKeys(keys as string[]),
        }}
      />
      <RunLogDrawer record={fullLogRecord} onClose={() => setFullLogRecord(null)} />
    </div>
  )
}

/** The kernel-attested account worth showing next to the (self-reported)
 *  operator — only when it adds something (differs from operator and run-as). */
/** What a run's installed-code integrity badge says (runspec >= 0.81), or null
 *  when there is nothing to report. */
export function integrityBadge(r: Pick<HistoryRecord, 'integrity' | 'integrityFiles' | 'host'>): { text: string; color: string; tip: string } | null {
  if (r.integrity === 'modified') {
    const files = r.integrityFiles ?? []
    const list = files.length ? ` Changed: ${files.slice(0, 5).join(', ')}${files.length > 5 ? ` and ${files.length - 5} more` : ''}.` : ''
    return {
      text: 'modified',
      color: 'error',
      tip: `This run used installed code that was changed after it was installed.${list} Run "runspec verify" on ${r.host || 'the host'} for details; reinstalling the package restores it.`,
    }
  }
  if (r.integrity === 'local') {
    return {
      text: 'local build',
      color: 'default',
      tip: 'Installed from a folder, a file or git rather than a package index, so this code may never have been committed or published.',
    }
  }
  return null
}

function IntegrityTag({ record }: { record: HistoryRecord }) {
  const badge = integrityBadge(record)
  if (!badge) return null
  const tone: StatusTone = badge.color === 'error' ? 'attention' : 'neutral'
  return <StatusTag tone={tone} tip={badge.tip} style={{ flex: '0 0 auto' }}>{badge.text}</StatusTag>
}

export function accountNote(r: Pick<HistoryRecord, 'account' | 'operator' | 'runAs'>): string {
  const a = (r.account || '').trim()
  if (!a || a === r.operator || a === r.runAs) return ''
  return a
}

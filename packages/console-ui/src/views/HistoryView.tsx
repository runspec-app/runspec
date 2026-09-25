import { useEffect, useMemo, useState } from 'react'
import { Table, Tag, Typography, Button, Tooltip, message, Input, Drawer, Spin, Empty, Alert, Segmented } from 'antd'
import { RedoOutlined, CopyOutlined, RobotOutlined, UserOutlined, SearchOutlined, EditOutlined, ClockCircleOutlined, ProfileOutlined } from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import { bridge, type HistoryRecord, type HistoryLogLine, type LogViewResult, type RunningRun } from '../bridge'
import { LogStream } from '../components/LogStream'
import { MONO_FONT, RADIUS, useAccent, withAlpha } from '../ThemeContext'

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

const LOG_LEVEL_COLOUR: Record<string, string> = {
  INFO: '#4fc1ff',
  WARNING: '#faad14',
  ERROR: '#ff4d4f',
  DEBUG: '#888',
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

  return (
    <div style={{ padding: '12px 24px 12px 48px', display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* Rerun controls */}
      {(onRerun || canViewFullLog) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {onRerun && (
          <Button size="small" icon={<RedoOutlined />} onClick={() => onRerun(record)}>
            Rerun
          </Button>
          )}
          {/* Always offered — not gated on recorded args. Secrets (password-typed
              args) are never written to the audit record, so a runnable whose
              required arg is a secret has empty `args` here; plain Rerun would
              fail with "missing required argument". The form is the way to supply
              it, so the edit path must stay reachable even with nothing recorded. */}
          {onRerun && (
          <Tooltip title="Open the run dialog pre-filled with this run's values — rich typed inputs and every available arg, including required secrets that aren't recorded">
            <Button size="small" icon={<EditOutlined />} onClick={editInForm}>
              Edit & rerun
            </Button>
          </Tooltip>
          )}
          {canViewFullLog && (
            <Tooltip title="Open the complete merged log for this run — every line (the inline preview may be truncated), read from the venv log on demand">
              <Button size="small" icon={<ProfileOutlined />} onClick={() => onViewFullLog!(record)}>
                View full log
              </Button>
            </Tooltip>
          )}
        </div>
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
              <Tooltip title="Copy to clipboard">
                <Button
                  type="text"
                  size="small"
                  icon={<CopyOutlined />}
                  onClick={handleCopy}
                  style={{ color: '#555', height: 22, padding: '0 5px' }}
                />
              </Tooltip>
              {onAskLlm && (
                <Tooltip title="Ask the assistant">
                  <Button
                    type="text"
                    size="small"
                    icon={<RobotOutlined />}
                    onClick={handleAskLlm}
                    style={{ color: '#555', height: 22, padding: '0 5px' }}
                  />
                </Tooltip>
              )}
            </div>
          )}
          {logsLoading
            ? <Spin size="small" />
            : logLines.length === 0
            ? <span style={{ color: '#555' }}>no log lines</span>
            : logLines.map((line, i) => <LogLine key={i} line={line} />)
          }
        </div>
      </div>
    </div>
  )
}

function LogLine({ line }: { line: HistoryLogLine }) {
  const colour = LOG_LEVEL_COLOUR[line.level] ?? '#d4d4d4'
  const time = new Date(line.ts).toLocaleTimeString()
  return (
    <div>
      <span style={{ color: '#555', marginRight: 10 }}>{time}</span>
      <span style={{ color: colour, marginRight: 10, minWidth: 56, display: 'inline-block' }}>{line.level}</span>
      <span style={{ color: '#d4d4d4' }}>{line.message}</span>
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
        <Empty description="No records" />
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
              {r.trigger === 'schedule' && <ClockCircleOutlined style={{ color: 'var(--rs-text-muted)', fontSize: 12 }} />}
              {r.initiatedBy === 'llm'
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
  const [hostColumnFilter, setHostColumnFilter] = useState<string[]>([])
  const [expandedKeys, setExpandedKeys] = useState<string[]>([])
  const [fullLogRecord, setFullLogRecord] = useState<HistoryRecord | null>(null)
  // Read window in days (0 = all) — bounds the backend read so a first visit is
  // quick. Changing it refetches (see the effect's deps).
  const [sinceDays, setSinceDays] = useState(7)
  // Per-run log lines fetched lazily when a row expands (the bulk payload ships
  // slim rows), keyed by rowKeyOf(r); detailLoading tracks the in-flight fetch.
  const [details, setDetails] = useState<Record<string, HistoryLogLine[]>>({})
  const [detailLoading, setDetailLoading] = useState<Record<string, boolean>>({})
  // Live "what's running" across the fleet (get_running_runs) — the strip above
  // the table. Refreshed on the same cadence as the history read below (initial +
  // 25 s poll + debounced run_end), so it costs no extra timer; the strip itself
  // ticks the per-run elapsed time.
  const [running, setRunning] = useState<RunningRun[]>([])
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
      Promise.resolve(bridge.get_running_runs())
        .then(rows => { if (!cancelled) setRunning(Array.isArray(rows) ? rows : []) })
        .catch(() => { if (!cancelled) setRunning([]) })
    }

    const runLoad = async (showSpinner: boolean) => {
      loadRunning()
      if (showSpinner) { setLoading(true); setRecordsByHost({}) }
      let hosts: string[]
      try { hosts = await bridge.history_targets() } catch { hosts = ['local'] }
      if (cancelled) return
      setTargets(hosts)
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
        const t0 = performance.now()
        bridge.get_history_slim(name, sinceDays)
          .then(rows => {
            if (cancelled) return
            setRecordsByHost(prev => ({ ...prev, [name]: rows }))
            setHostStatus(prev => ({ ...prev, [name]: { status: 'done', ms: Math.round(performance.now() - t0), count: rows.length } }))
          })
          .catch(e => {
            if (cancelled) return
            setHostStatus(prev => ({ ...prev, [name]: { status: 'error', ms: Math.round(performance.now() - t0), error: String(e) } }))
          })
          .finally(() => { if (!cancelled) { pending -= 1; if (pending === 0) setLoading(false) } })
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

    return () => {
      cancelled = true
      clearInterval(id)
      if (debounce) clearTimeout(debounce)
      window.removeEventListener('runspec:run_end', onRunEnd)
    }
  }, [active, sinceDays])

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
    if (!record.runId || details[key] !== undefined || detailLoading[key]) return
    setDetailLoading(d => ({ ...d, [key]: true }))
    bridge.get_run_detail(record.host, record.runId)
      .then(res => setDetails(d => ({ ...d, [key]: res.logLines ?? [] })))
      .catch(() => setDetails(d => ({ ...d, [key]: [] })))
      .finally(() => setDetailLoading(d => { const n = { ...d }; delete n[key]; return n }))
  }

  const filtered = useMemo(() => {
    if (!search) return records
    const q = search.toLowerCase()
    return records.filter(r =>
      r.runnable.toLowerCase().includes(q) ||
      r.group.toLowerCase().includes(q) ||
      r.host.toLowerCase().includes(q) ||
      r.operator.toLowerCase().includes(q) ||
      r.runAs.toLowerCase().includes(q)
    )
  }, [records, search])

  const facets = useMemo(() => ({
    hosts: [...new Set(records.map(r => r.host))],
    groups: [...new Set(records.map(r => r.group))],
    operators: [...new Set(records.map(r => r.operator))],
    runAsUsers: [...new Set(records.map(r => r.runAs))],
    runbooks: [...new Set(records.map(r => r.runbook).filter((s): s is string => !!s))],
  }), [records])
  const { hosts, groups, operators, runAsUsers, runbooks } = facets

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
        const full = `${r.group ? r.group + '/' : ''}${r.runnable}`
        return (
          <span style={{ fontFamily: MONO_FONT, fontSize: 13, display: 'inline-flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
            {r.trigger === 'schedule' && (
              <Tooltip title="Ran on a schedule">
                <ClockCircleOutlined style={{ color: 'var(--rs-text-muted)', fontSize: 13, flex: '0 0 auto' }} />
              </Tooltip>
            )}
            <Tooltip title={full}>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {r.group && <span style={{ color: 'var(--rs-text-muted)' }}>{r.group}/</span>}{r.runnable}
              </span>
            </Tooltip>
          </span>
        )
      },
      sorter: (a, b) => `${a.group}/${a.runnable}`.localeCompare(`${b.group}/${b.runnable}`),
    },
    {
      // Dedicated column so "run by agent vs operator" is always visible — the
      // old inline robot icon lived at the end of the ellipsized Runnable cell
      // and was the first thing truncated on a narrow window (the reported
      // "can't see if run by agent or user").
      title: 'Run by',
      key: 'runBy',
      width: 100,
      render: (_: unknown, r: HistoryRecord) => r.initiatedBy === 'llm' ? (
        <Tooltip title="Triggered by the assistant">
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color: accent }}>
            <RobotOutlined style={{ fontSize: 13 }} />agent
          </span>
        </Tooltip>
      ) : (
        <Tooltip title="Run by an operator">
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color: 'var(--rs-text-muted)' }}>
            <UserOutlined style={{ fontSize: 13 }} />user
          </span>
        </Tooltip>
      ),
      filters: [{ text: 'Agent', value: 'llm' }, { text: 'User', value: 'user' }],
      onFilter: (value, record) => (record.initiatedBy ?? 'user') === value,
      filteredValue: [],
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
            onClick={() => onScopeToggle(g)}
            style={{ cursor: 'pointer', fontWeight: active ? 600 : 400 }}
            title={active ? 'Remove from scope' : 'Add to scope'}
          >
            {g}
          </Tag>
        )
      },
      filters: groups.map(g => ({ text: g, value: g })),
      onFilter: (value, record) => record.group === value,
      filteredValue: [],
    },
    ...(runbooks.length > 0 ? [{
      title: 'Runbook',
      key: 'runbook',
      render: (_: unknown, r: HistoryRecord) => r.runbook
        ? <Tag color="purple" style={{ fontFamily: MONO_FONT }}>${r.runbook}</Tag>
        : <span style={{ color: 'var(--rs-text-faint)' }}>—</span>,
      filters: runbooks.map(s => ({ text: `$${s}`, value: s })),
      onFilter: (value, record) => record.runbook === value,
    } as ColumnType<HistoryRecord>] : []),
    {
      title: 'Host',
      dataIndex: 'host',
      key: 'host',
      render: (h: string) => <Tag>{h}</Tag>,
      filters: hosts.map(h => ({ text: h, value: h })),
      onFilter: (value, record) => record.host === value,
      filteredValue: hostColumnFilter,
    },
    {
      title: 'Operator',
      dataIndex: 'operator',
      key: 'operator',
      filters: operators.map(u => ({ text: u, value: u })),
      onFilter: (value, record) => record.operator === value,
      filteredValue: [],
    },
    {
      title: 'Run as',
      dataIndex: 'runAs',
      key: 'runAs',
      render: (u: string) => <code style={{ fontSize: 13 }}>{u}</code>,
      filters: runAsUsers.map(u => ({ text: u, value: u })),
      onFilter: (value, record) => record.runAs === value,
      filteredValue: [],
    },
    {
      title: 'Status',
      dataIndex: 'exitCode',
      key: 'exitCode',
      render: (c: number) => <Tag color={c === 0 ? 'green' : 'red'}>{c === 0 ? 'ok' : `exit ${c}`}</Tag>,
      filters: [
        { text: 'ok', value: 0 },
        { text: 'error', value: 1 },
      ],
      onFilter: (value, record) => (value === 0 ? record.exitCode === 0 : record.exitCode !== 0),
      filteredValue: [],
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
  ], [groups, runbooks, hosts, operators, runAsUsers, hostColumnFilter, activeScope, onScopeToggle, accent])

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16 }}>
        <Input
          prefix={<SearchOutlined />}
          placeholder="Search runnable, host, operator, run-as…"
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
      </div>
      {/* Live "Running now" strip — in-flight runs from the runs index (see
          RunningNowStrip). Shown only when something is running so it stays
          unobtrusive; pinned above the finished-runs table. */}
      {running.length > 0 && <RunningNowStrip rows={running} accent={accent} />}
      {/* Per-host fetch strip — one chip per host with its run count and fetch
          time (amber when slow, red on error), so a slow SSH connection or an
          offline host is visible instead of hiding in one fleet-wide spinner.
          Only shown when reading more than the local host. */}
      {targets.length > 1 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 12, alignItems: 'center' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>Per-host fetch:</Text>
          {targets.map(name => {
            const s = hostStatus[name]
            const st = s?.status ?? 'loading'
            const colour = st === 'error' ? '#ff4d4f'
              : st === 'loading' ? 'var(--rs-text-faint)'
              : slowMs(s?.ms) ? '#faad14' : 'var(--rs-text-muted)'
            const detail = st === 'loading' ? '…' : st === 'error' ? 'failed' : `${s?.count ?? 0}·${fmtMs(s?.ms)}`
            const tip = st === 'error' ? (s?.error || 'fetch failed')
              : st === 'loading' ? 'fetching…'
              : `${s?.count ?? 0} run(s) in ${fmtMs(s?.ms)}${slowMs(s?.ms) ? ' — slow' : ''}`
            return (
              <Tooltip key={name} title={tip}>
                <span style={{ fontSize: 12, fontFamily: MONO_FONT, color: colour, border: '1px solid var(--rs-border)', borderRadius: 4, padding: '1px 6px', whiteSpace: 'nowrap' }}>
                  {name} <span style={{ opacity: 0.7 }}>{detail}</span>
                </span>
              </Tooltip>
            )
          })}
        </div>
      )}
      <Table
        dataSource={filtered}
        columns={columns}
        rowKey={rowKeyOf}
        size="small"
        loading={loading && records.length === 0}
        pagination={{ pageSize: 50, showSizeChanger: true }}
        onChange={(_, filters) => setHostColumnFilter((filters['host'] as string[]) || [])}
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

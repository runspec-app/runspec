import { useEffect, useMemo, useRef, useState } from 'react'
import { Table, Tag, Typography, Button, Tooltip, message, Input, Drawer, Spin, Empty, Alert } from 'antd'
import { RedoOutlined, CopyOutlined, RobotOutlined, SearchOutlined, EditOutlined, ClockCircleOutlined, ProfileOutlined } from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import { bridge, type HistoryRecord, type HistoryLogLine, type LogViewResult } from '../bridge'
import { LogStream } from '../components/LogStream'

const { Title, Text } = Typography

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
      <Tag color={badge.color} style={{ fontSize: 10, margin: '0 0 0 2px', padding: '0 4px', lineHeight: '16px', cursor: 'default' }}>
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
  onRerun?: (record: HistoryRecord) => void
  onAskLlm?: (text: string) => void
  onViewFullLog?: (record: HistoryRecord) => void
}

function ExpandedRun({ record, onRerun, onAskLlm, onViewFullLog }: ExpandedRunProps) {
  const args = record.args ?? {}
  const logLines = record.logLines ?? []
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
        <Text type="secondary" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: 1 }}>
          Arguments
        </Text>
        {hasArgs ? (
          <div style={{ marginTop: 6, display: 'flex', flexWrap: 'wrap', gap: '6px 16px', alignItems: 'center' }}>
            {Object.entries(args).map(([k, v]) => (
              <span key={k} style={{ display: 'inline-flex', alignItems: 'center' }}>
                <Text type="secondary" style={{ fontSize: 12 }}>--{k}=</Text>
                <Text code style={{ fontSize: 12 }}>{String(v)}</Text>
                <ArgSourceBadge source={record.argSources?.[k]} />
              </span>
            ))}
          </div>
        ) : (
          <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>none</Text>
        )}
      </div>

      {/* Log lines */}
      <div>
        <Text type="secondary" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: 1 }}>
          Log
        </Text>
        <div style={{
          position: 'relative',
          marginTop: 6,
          background: '#0d0d0d', border: '1px solid #222', borderRadius: 6,
          padding: '8px 12px', fontFamily: 'monospace', fontSize: 12, lineHeight: 1.8,
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
          {logLines.length === 0
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
      width={680}
      title={record ? <span style={{ fontFamily: 'monospace' }}>/{record.runnable} — full log ({(record.runId ?? '').slice(0, 8)})</span> : ''}
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

interface HistoryViewProps {
  search: string
  onSearchChange: (s: string) => void
  onRerun?: (record: HistoryRecord) => void
  onAskLlm?: (text: string) => void
  activeScope: string[]
  onScopeToggle: (group: string) => void
  selectedHost: string
  active: boolean
}

export function HistoryView({ search, onSearchChange, onRerun, onAskLlm, activeScope, onScopeToggle, selectedHost, active }: HistoryViewProps) {
  const [records, setRecords] = useState<HistoryRecord[]>([])
  const [loading, setLoading] = useState(false)
  // History is scoped to the host selected in the sidebar — clicking a host
  // drives History (and Analytics) for it. One host per read keeps each fetch a
  // single SSH round-trip instead of a fleet-wide fan-out that hangs on a slow
  // or offline host.
  const [hostColumnFilter, setHostColumnFilter] = useState<string[]>([])
  const [expandedKeys, setExpandedKeys] = useState<string[]>([])
  const [fullLogRecord, setFullLogRecord] = useState<HistoryRecord | null>(null)

  // Switching host drops the old host's rows, so we never briefly show another
  // host's runs and the reload below shows the spinner instead of stale data.
  // Returning to the same host keeps its rows, so the tab re-shows instantly.
  useEffect(() => { setRecords([]) }, [selectedHost])

  // The view stays mounted across tab switches (App toggles display), so records
  // persist — returning shows the last data instantly. Reads only happen while
  // visible. The backend read is now cheap (bounded newest-N + mtime cache), so
  // the primary refresh is event-driven: a completed run fires runspec:run_end,
  // and a debounced silent refetch pulls just the changed file. A slow fallback
  // poll (25 s) catches anything the event path misses (a run on another venv,
  // a missed event). The spinner shows on the first (re)load only.
  useEffect(() => {
    if (!selectedHost || !active) return
    let cancelled = false
    let debounce: ReturnType<typeof setTimeout> | undefined

    // Spinner shows only here (showSpinner=true is passed once, on mount/host
    // change, while records is empty); silent polls and event refetches pass
    // false so they never flash it.
    const fetch = (showSpinner: boolean) => {
      if (showSpinner) setLoading(true)
      return bridge.get_history(selectedHost)
        .then(r => { if (!cancelled) setRecords(r) })
        .catch(() => {})
        .finally(() => { if (!cancelled) setLoading(false) })
    }

    fetch(true)
    const id = setInterval(() => fetch(false), 25000)

    // A run just ended → its run_summary is now on disk. Debounce so a burst of
    // events (a group fan-out) triggers a single refetch. Never shows a spinner.
    const onRunEnd = () => {
      if (debounce) clearTimeout(debounce)
      debounce = setTimeout(() => { if (!cancelled) fetch(false) }, 750)
    }
    window.addEventListener('runspec:run_end', onRunEnd)

    return () => {
      cancelled = true
      clearInterval(id)
      if (debounce) clearTimeout(debounce)
      window.removeEventListener('runspec:run_end', onRunEnd)
    }
  }, [selectedHost, active])

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
      render: (_: unknown, r: HistoryRecord) => (
        <span style={{ fontFamily: 'monospace', fontSize: 12, display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <span><span style={{ color: '#666' }}>{r.group}/</span>{r.runnable}</span>
          {r.trigger === 'schedule' && (
            <Tooltip title="Ran on a schedule">
              <ClockCircleOutlined style={{ color: '#8c8c8c', fontSize: 12 }} />
            </Tooltip>
          )}
          {r.initiatedBy === 'llm' && (
            <Tooltip title="Triggered by the assistant">
              <RobotOutlined style={{ color: '#1677ff', fontSize: 12 }} />
            </Tooltip>
          )}
        </span>
      ),
      sorter: (a, b) => `${a.group}/${a.runnable}`.localeCompare(`${b.group}/${b.runnable}`),
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
        ? <Tag color="purple" style={{ fontFamily: 'monospace' }}>${r.runbook}</Tag>
        : <span style={{ color: '#999' }}>—</span>,
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
      render: (u: string) => <code style={{ fontSize: 12 }}>{u}</code>,
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
  ], [groups, runbooks, hosts, operators, runAsUsers, hostColumnFilter, activeScope, onScopeToggle])

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
      </div>
      <Table
        dataSource={filtered}
        columns={columns}
        rowKey="id"
        size="small"
        loading={loading && records.length === 0}
        pagination={{ pageSize: 50, showSizeChanger: true }}
        onChange={(_, filters) => setHostColumnFilter((filters['host'] as string[]) || [])}
        expandable={{
          expandedRowRender: (record) => (
            <ExpandedRun record={record} onRerun={onRerun} onAskLlm={onAskLlm} onViewFullLog={setFullLogRecord} />
          ),
          rowExpandable: () => true,
          expandedRowKeys: expandedKeys,
          onExpandedRowsChange: (keys) => setExpandedKeys(keys as string[]),
        }}
      />
      <RunLogDrawer record={fullLogRecord} onClose={() => setFullLogRecord(null)} />
    </div>
  )
}

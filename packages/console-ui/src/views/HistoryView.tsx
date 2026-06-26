import { useEffect, useState } from 'react'
import { Table, Tag, Typography, Button, Tooltip, message, Input, Space } from 'antd'
import { RedoOutlined, CopyOutlined, RobotOutlined, SearchOutlined, EditOutlined, CaretRightOutlined, ClockCircleOutlined } from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import { bridge, type HistoryRecord, type HistoryLogLine } from '../bridge'

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
}

function ExpandedRun({ record, onRerun, onAskLlm }: ExpandedRunProps) {
  const args = record.args ?? {}
  const logLines = record.logLines ?? []
  const hasArgs = Object.keys(args).length > 0
  const [isEditing, setIsEditing] = useState(false)
  const [editArgs, setEditArgs] = useState<Record<string, string>>({})

  const startEditing = () => {
    setEditArgs(Object.fromEntries(Object.entries(args).map(([k, v]) => [k, String(v)])))
    setIsEditing(true)
  }

  const handleEditRun = () => {
    onRerun?.({ ...record, args: editArgs })
    setIsEditing(false)
  }

  const handleCopy = () => {
    navigator.clipboard.writeText(formatLogsAsText(record, logLines))
    message.success('Copied to clipboard')
  }

  const handleAskLlm = () => {
    onAskLlm?.(formatLogsAsText(record, logLines))
  }

  return (
    <div style={{ padding: '12px 24px 12px 48px', display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* Rerun controls */}
      {onRerun && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Button size="small" icon={<RedoOutlined />} onClick={() => onRerun(record)}>
            Rerun
          </Button>
          {hasArgs && !isEditing && (
            <Button size="small" icon={<EditOutlined />} onClick={startEditing}>
              Edit args
            </Button>
          )}
        </div>
      )}

      {/* Args — editable form or static display */}
      <div>
        <Text type="secondary" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: 1 }}>
          Arguments
        </Text>
        {isEditing ? (
          <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 8 }}>
            {Object.entries(editArgs).map(([k, v]) => (
              <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <Text code style={{ fontSize: 12, minWidth: 140, flexShrink: 0 }}>--{k}</Text>
                <Input
                  size="small"
                  value={v}
                  onChange={e => setEditArgs(a => ({ ...a, [k]: e.target.value }))}
                  style={{ maxWidth: 340, fontFamily: 'monospace', fontSize: 12 }}
                />
              </div>
            ))}
            <Space style={{ marginTop: 4 }}>
              <Button size="small" type="primary" icon={<CaretRightOutlined />} onClick={handleEditRun}>
                Run with edits
              </Button>
              <Button size="small" onClick={() => setIsEditing(false)}>Cancel</Button>
            </Space>
          </div>
        ) : hasArgs ? (
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
                <Tooltip title="Ask the LLM">
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

  // Switching host drops the old host's rows, so we never briefly show another
  // host's runs and the reload below shows the spinner instead of stale data.
  // Returning to the same host keeps its rows, so the tab re-shows instantly.
  useEffect(() => { setRecords([]) }, [selectedHost])

  // The view stays mounted across tab switches (App toggles display), so records
  // persist — returning shows the last data instantly. Poll only while visible
  // so a hidden History tab isn't doing an SSH round-trip every 8 s in the
  // background. The spinner shows on the visible (re)load, not on silent polls.
  useEffect(() => {
    if (!selectedHost || !active) return
    let cancelled = false
    const fetch = (showSpinner: boolean) => {
      if (showSpinner) setLoading(true)
      return bridge.get_history(selectedHost)
        .then(r => { if (!cancelled) setRecords(r) })
        .catch(() => {})
        .finally(() => { if (!cancelled) setLoading(false) })
    }
    fetch(true)
    const id = setInterval(() => fetch(false), 8000)
    return () => { cancelled = true; clearInterval(id) }
  }, [selectedHost, active])

  const filtered = search
    ? records.filter(r =>
        r.runnable.toLowerCase().includes(search.toLowerCase()) ||
        r.group.toLowerCase().includes(search.toLowerCase()) ||
        r.host.toLowerCase().includes(search.toLowerCase()) ||
        r.operator.toLowerCase().includes(search.toLowerCase()) ||
        r.runAs.toLowerCase().includes(search.toLowerCase())
      )
    : records

  const hosts = [...new Set(records.map(r => r.host))]
  const groups = [...new Set(records.map(r => r.group))]
  const operators = [...new Set(records.map(r => r.operator))]
  const runAsUsers = [...new Set(records.map(r => r.runAs))]
  const runbooks = [...new Set(records.map(r => r.runbook).filter((s): s is string => !!s))]

  const columns: ColumnType<HistoryRecord>[] = [
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
            <Tooltip title="Triggered by LLM">
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
  ]

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
            <ExpandedRun record={record} onRerun={onRerun} onAskLlm={onAskLlm} />
          ),
          rowExpandable: () => true,
          expandedRowKeys: expandedKeys,
          onExpandedRowsChange: (keys) => setExpandedKeys(keys as string[]),
        }}
      />
    </div>
  )
}

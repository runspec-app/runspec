import { useEffect, useState } from 'react'
import { Table, Tag, Typography, Button, Tooltip, message, Input, Space } from 'antd'
import { RedoOutlined, CopyOutlined, RobotOutlined, SearchOutlined, EditOutlined, CaretRightOutlined } from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import { bridge, type HistoryRecord, type HistoryLogLine } from '../bridge'

const { Title, Text } = Typography

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
  rerunCount: number
  onRerun?: (record: HistoryRecord) => void
  onAskLlm?: (text: string) => void
}

function ExpandedRun({ record, rerunCount, onRerun, onAskLlm }: ExpandedRunProps) {
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
          {rerunCount > 0 && (
            <Text type="secondary" style={{ fontSize: 11 }}>× {rerunCount}</Text>
          )}
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
          <div style={{ marginTop: 6, display: 'flex', flexWrap: 'wrap', gap: '6px 16px' }}>
            {Object.entries(args).map(([k, v]) => (
              <span key={k}>
                <Text type="secondary" style={{ fontSize: 12 }}>--{k}=</Text>
                <Text code style={{ fontSize: 12 }}>{String(v)}</Text>
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
  onRerun?: (record: HistoryRecord) => void
  onAskLlm?: (text: string) => void
}

export function HistoryView({ onRerun, onAskLlm }: HistoryViewProps) {
  const [records, setRecords] = useState<HistoryRecord[]>([])
  const [search, setSearch] = useState('')
  const [rerunCounts, setRerunCounts] = useState<Record<string, number>>({})

  const handleRerun = (record: HistoryRecord) => {
    setRerunCounts(c => ({ ...c, [record.id]: (c[record.id] ?? 0) + 1 }))
    onRerun?.(record)
  }

  useEffect(() => {
    bridge.get_history('local').then(setRecords)
  }, [])

  const filtered = search
    ? records.filter(r =>
        r.runnable.toLowerCase().includes(search.toLowerCase()) ||
        r.host.toLowerCase().includes(search.toLowerCase()) ||
        r.operator.toLowerCase().includes(search.toLowerCase()) ||
        r.runAs.toLowerCase().includes(search.toLowerCase())
      )
    : records

  const hosts = [...new Set(records.map(r => r.host))]
  const operators = [...new Set(records.map(r => r.operator))]
  const runAsUsers = [...new Set(records.map(r => r.runAs))]

  const columns: ColumnType<HistoryRecord>[] = [
    {
      title: 'Runnable',
      dataIndex: 'runnable',
      key: 'runnable',
      render: (n: string) => <code>/{n}</code>,
      sorter: (a, b) => a.runnable.localeCompare(b.runnable),
    },
    {
      title: 'Host',
      dataIndex: 'host',
      key: 'host',
      render: (h: string) => <Tag>{h}</Tag>,
      filters: hosts.map(h => ({ text: h, value: h })),
      onFilter: (value, record) => record.host === value,
    },
    {
      title: 'Operator',
      dataIndex: 'operator',
      key: 'operator',
      filters: operators.map(u => ({ text: u, value: u })),
      onFilter: (value, record) => record.operator === value,
    },
    {
      title: 'Run as',
      dataIndex: 'runAs',
      key: 'runAs',
      render: (u: string) => <code style={{ fontSize: 12 }}>{u}</code>,
      filters: runAsUsers.map(u => ({ text: u, value: u })),
      onFilter: (value, record) => record.runAs === value,
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
      <Title level={4} style={{ marginBottom: 16 }}>History</Title>
      <Input
        prefix={<SearchOutlined />}
        placeholder="Search runnable, host, operator, run-as…"
        value={search}
        onChange={e => setSearch(e.target.value)}
        allowClear
        style={{ marginBottom: 16, maxWidth: 400 }}
      />
      <Table
        dataSource={filtered}
        columns={columns}
        rowKey="id"
        size="small"
        pagination={{ pageSize: 50, showSizeChanger: true }}
        expandable={{
          expandedRowRender: (record) => (
            <ExpandedRun
              record={record}
              rerunCount={rerunCounts[record.id] ?? 0}
              onRerun={handleRerun}
              onAskLlm={onAskLlm}
            />
          ),
          rowExpandable: () => true,
        }}
      />
    </div>
  )
}

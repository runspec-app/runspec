import { useEffect, useState } from 'react'
import { Table, Tag, Typography, Button, Tooltip, message, Input } from 'antd'
import { RedoOutlined, CopyOutlined, RobotOutlined, SearchOutlined } from '@ant-design/icons'
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
  onRerun?: (record: HistoryRecord) => void
  onAskLlm?: (text: string) => void
}

function ExpandedRun({ record, onRerun, onAskLlm }: ExpandedRunProps) {
  const args = record.args ?? {}
  const logLines = record.logLines ?? []
  const hasArgs = Object.keys(args).length > 0

  const handleCopy = () => {
    navigator.clipboard.writeText(formatLogsAsText(record, logLines))
    message.success('Copied to clipboard')
  }

  const handleAskLlm = () => {
    onAskLlm?.(formatLogsAsText(record, logLines))
  }

  return (
    <div style={{ padding: '12px 24px 12px 48px', display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* Rerun */}
      {onRerun && (
        <div>
          <Button
            size="small"
            icon={<RedoOutlined />}
            onClick={() => onRerun(record)}
          >
            Rerun with same args
          </Button>
        </div>
      )}

      {/* Args */}
      <div>
        <Text type="secondary" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: 1 }}>
          Arguments
        </Text>
        {hasArgs ? (
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
            <ExpandedRun record={record} onRerun={onRerun} onAskLlm={onAskLlm} />
          ),
          rowExpandable: () => true,
        }}
      />
    </div>
  )
}

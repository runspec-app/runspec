import { useEffect, useState } from 'react'
import { Table, Tag, Typography } from 'antd'
import { bridge, type HistoryRecord } from '../bridge'

const { Title } = Typography

export function HistoryView() {
  const [records, setRecords] = useState<HistoryRecord[]>([])

  useEffect(() => {
    bridge.get_history('local').then(setRecords)
  }, [])

  const columns = [
    { title: 'Runnable', dataIndex: 'runnable', key: 'runnable', render: (n: string) => <code>/{n}</code> },
    { title: 'Host', dataIndex: 'host', key: 'host', render: (h: string) => <Tag>{h}</Tag> },
    {
      title: 'Status', dataIndex: 'exitCode', key: 'exitCode',
      render: (c: number) => <Tag color={c === 0 ? 'green' : 'red'}>{c === 0 ? 'ok' : `exit ${c}`}</Tag>
    },
    {
      title: 'Duration', dataIndex: 'durationMs', key: 'durationMs',
      render: (ms: number) => `${(ms / 1000).toFixed(2)}s`
    },
    {
      title: 'Time', dataIndex: 'ts', key: 'ts',
      render: (ts: string) => new Date(ts).toLocaleString()
    },
  ]

  return (
    <div>
      <Title level={4} style={{ marginBottom: 16 }}>History</Title>
      <Table dataSource={records} columns={columns} rowKey="id" size="small" />
    </div>
  )
}

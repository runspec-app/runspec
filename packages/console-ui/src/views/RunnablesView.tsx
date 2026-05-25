import { useEffect, useState } from 'react'
import { Table, Tag, Input, Typography } from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import { bridge, type Runnable } from '../bridge'

const { Title } = Typography

export function RunnablesView() {
  const [runnables, setRunnables] = useState<Runnable[]>([])
  const [search, setSearch] = useState('')

  useEffect(() => {
    bridge.get_runnables('local').then(setRunnables)
  }, [])

  const filtered = runnables.filter(r =>
    r.name.includes(search) || r.host.includes(search) || (r.description ?? '').toLowerCase().includes(search.toLowerCase())
  )

  const columns = [
    { title: 'Runnable', dataIndex: 'name', key: 'name', render: (n: string) => <code>/{n}</code> },
    { title: 'Host', dataIndex: 'host', key: 'host', render: (h: string) => <Tag>{h}</Tag> },
    { title: 'Description', dataIndex: 'description', key: 'description', render: (d: string) => d ?? <span style={{ color: '#555' }}>—</span> },
    { title: 'Args', key: 'args', render: (_: unknown, r: Runnable) => r.args.length },
  ]

  return (
    <div>
      <Title level={4} style={{ marginBottom: 16 }}>Runnables</Title>
      <Input
        prefix={<SearchOutlined />}
        placeholder="Filter runnables..."
        value={search}
        onChange={e => setSearch(e.target.value)}
        style={{ marginBottom: 16, maxWidth: 360 }}
      />
      <Table dataSource={filtered} columns={columns} rowKey={r => `${r.host}/${r.name}`} size="small" />
    </div>
  )
}

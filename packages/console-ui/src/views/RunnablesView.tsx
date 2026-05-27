import { useEffect, useState } from 'react'
import { Table, Tag, Input, Typography } from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import type { ColumnType } from 'antd/es/table'
import type { Runnable, ArgDef } from '../bridge'

const { Text } = Typography

function isVarRef(val: unknown): val is string {
  return typeof val === 'string' && /^\$[A-Z_][A-Z0-9_]*$/.test(val)
}

function RunnableHelp({ runnable }: { runnable: Runnable }) {
  if ((runnable.args ?? []).length === 0) {
    return <Text type="secondary" style={{ fontSize: 12, padding: '8px 48px', display: 'block' }}>No arguments.</Text>
  }

  return (
    <div style={{ padding: '10px 48px 14px' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {(runnable.args ?? []).map((arg: ArgDef) => (
          <div key={arg.name} style={{ display: 'flex', alignItems: 'baseline', gap: 10, fontFamily: 'monospace', fontSize: 12 }}>
            <Text code style={{ fontSize: 12, minWidth: 140 }}>--{arg.name}</Text>
            <Tag color="default" style={{ fontSize: 11 }}>{arg.type}</Tag>
            {arg.required ? (
              <Tag color="orange" style={{ fontSize: 11 }}>required</Tag>
            ) : isVarRef(arg.default) ? (
              <Text style={{ fontSize: 11, color: '#fa8c16', fontFamily: 'monospace' }}>{arg.default}</Text>
            ) : (
              <Text type="secondary" style={{ fontSize: 11 }}>
                optional{arg.default !== undefined ? ` · default: ${String(arg.default)}` : ''}
              </Text>
            )}
            {arg.description && (
              <Text type="secondary" style={{ fontSize: 12, marginLeft: 4 }}>{arg.description}</Text>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

interface RunnablesViewProps {
  runnables: Runnable[]
  selectedHost: string
  activeScope: string[]
  onScopeToggle: (group: string) => void
}

export function RunnablesView({ runnables, selectedHost, activeScope, onScopeToggle }: RunnablesViewProps) {
  const [search, setSearch] = useState('')
  const [hostColumnFilter, setHostColumnFilter] = useState<string[]>(selectedHost ? [selectedHost] : [])

  useEffect(() => {
    setHostColumnFilter(selectedHost ? [selectedHost] : [])
  }, [selectedHost])

  const filtered = search
    ? runnables.filter(r =>
        r.name.toLowerCase().includes(search.toLowerCase()) ||
        r.group.toLowerCase().includes(search.toLowerCase()) ||
        r.host.toLowerCase().includes(search.toLowerCase()) ||
        (r.description ?? '').toLowerCase().includes(search.toLowerCase())
      )
    : runnables

  const hosts = [...new Set(runnables.map(r => r.host))]
  const groups = [...new Set(runnables.map(r => r.group))]

  const columns: ColumnType<Runnable>[] = [
    {
      title: 'Runnable',
      key: 'name',
      render: (_: unknown, r: Runnable) => (
        <span style={{ fontFamily: 'monospace', fontSize: 13 }}>
          <span style={{ color: '#666', fontSize: 12 }}>{r.group}/</span>{r.name}
        </span>
      ),
      sorter: (a, b) => `${a.group}/${a.name}`.localeCompare(`${b.group}/${b.name}`),
    },
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
    {
      title: 'Description',
      dataIndex: 'description',
      key: 'description',
      render: (d: string) => d ?? <span style={{ color: '#555' }}>—</span>,
    },
    {
      title: 'Args',
      key: 'args',
      render: (_: unknown, r: Runnable) => {
        const count = (r.args ?? []).length
        const required = (r.args ?? []).filter(a => a.required).length
        if (count === 0) return <Text type="secondary" style={{ fontSize: 12 }}>none</Text>
        return <Text style={{ fontSize: 12 }}>{count} ({required} required)</Text>
      },
    },
  ]

  return (
    <div>
      <Input
        prefix={<SearchOutlined />}
        placeholder="Search name, group, host, description…"
        value={search}
        onChange={e => setSearch(e.target.value)}
        style={{ marginBottom: 16, maxWidth: 360 }}
      />
      <Table
        dataSource={filtered}
        columns={columns}
        rowKey={r => `${r.host}/${r.group}/${r.name}`}
        size="small"
        onChange={(_, filters) => setHostColumnFilter((filters['host'] as string[]) || [])}
        expandable={{
          expandedRowRender: (r) => <RunnableHelp runnable={r} />,
          rowExpandable: (r) => (r.args ?? []).length >= 0,
          expandRowByClick: false,
        }}
      />
    </div>
  )
}

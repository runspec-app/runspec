import { useMemo, useState } from 'react'
import { Tree, Tag, Typography, Input } from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import type { DataNode } from 'antd/es/tree'
import type { Host, Runnable, ArgDef } from '../bridge'

const { Text } = Typography

function varRef(val: unknown): string | null {
  if (typeof val === 'string' && /^\$[A-Z_][A-Z0-9_]*$/.test(val)) return val
  return null
}

function HostTitle({ host }: { host: Host }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
      <span style={{ fontSize: 9, color: host.connected ? '#52c41a' : '#595959' }}>●</span>
      <Text strong style={{ fontFamily: 'monospace', fontSize: 13 }}>{host.name}</Text>
      {host.role && (
        <Tag color={host.role === 'primary' ? 'geekblue' : 'default'} style={{ fontSize: 11, margin: 0 }}>
          {host.role}
        </Tag>
      )}
      {!host.connected && (
        <Text type="secondary" style={{ fontSize: 11 }}>disconnected</Text>
      )}
    </span>
  )
}

function GroupTitle({ group, count, active, onToggle }: {
  group: string
  count: number
  active: boolean
  onToggle: () => void
}) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
      <Tag
        color={active ? 'geekblue' : 'blue'}
        onClick={e => { e.stopPropagation(); onToggle() }}
        style={{ cursor: 'pointer', fontWeight: active ? 600 : 400, margin: 0 }}
        title={active ? 'Remove from scope' : 'Add to scope'}
      >
        {group}
      </Tag>
      <Text type="secondary" style={{ fontSize: 11 }}>{count} {count === 1 ? 'runnable' : 'runnables'}</Text>
    </span>
  )
}

function RunnableTitle({ runnable }: { runnable: Runnable }) {
  const reqCount = runnable.args.filter(a => a.required).length
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
      <Text code style={{ fontSize: 12 }}>{runnable.name}</Text>
      {runnable.description && (
        <Text type="secondary" style={{ fontSize: 12 }}>{runnable.description}</Text>
      )}
      {runnable.args.length > 0 && (
        <Text type="secondary" style={{ fontSize: 11 }}>
          {runnable.args.length} {runnable.args.length === 1 ? 'arg' : 'args'}
          {reqCount > 0 ? ` · ${reqCount} required` : ''}
        </Text>
      )}
    </span>
  )
}

function ArgTitle({ arg }: { arg: ArgDef }) {
  const ref = varRef(arg.default)
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
      <Text code style={{ fontSize: 12, minWidth: 130, flexShrink: 0 }}>--{arg.name}</Text>
      <Tag color="default" style={{ fontSize: 11, margin: 0 }}>{arg.type}</Tag>
      {arg.required ? (
        <Tag color="orange" style={{ fontSize: 11, margin: 0 }}>required</Tag>
      ) : ref ? (
        <Text style={{ fontSize: 11, color: '#fa8c16', fontFamily: 'monospace' }}>{ref}</Text>
      ) : (
        <Text type="secondary" style={{ fontSize: 11 }}>
          optional{arg.default !== undefined ? ` · ${String(arg.default)}` : ''}
        </Text>
      )}
      {arg.description && (
        <Text type="secondary" style={{ fontSize: 12 }}>{arg.description}</Text>
      )}
    </span>
  )
}

interface HostsViewProps {
  hosts: Host[]
  runnables: Runnable[]
  selectedHost: string
  activeScope: string[]
  onScopeToggle: (group: string) => void
}

export function HostsView({ hosts, runnables, selectedHost, activeScope, onScopeToggle }: HostsViewProps) {
  const [search, setSearch] = useState('')

  const { treeData, defaultExpandedKeys } = useMemo(() => {
    const q = search.toLowerCase()

    const displayHosts = selectedHost ? hosts.filter(h => h.name === selectedHost) : hosts

    const matchedRunnables = q
      ? runnables.filter(r =>
          r.name.toLowerCase().includes(q) ||
          r.group.toLowerCase().includes(q) ||
          (r.description ?? '').toLowerCase().includes(q) ||
          r.args.some(a =>
            a.name.toLowerCase().includes(q) ||
            (a.description ?? '').toLowerCase().includes(q)
          )
        )
      : runnables

    const expandedKeys: string[] = []

    const data: DataNode[] = displayHosts.map(host => {
      expandedKeys.push(host.name)
      const hostRunnables = matchedRunnables.filter(r => r.host === host.name)
      const groups = [...new Set(hostRunnables.map(r => r.group))]

      return {
        key: host.name,
        title: <HostTitle host={host} />,
        children: groups.map(group => {
          const groupKey = `${host.name}/${group}`
          expandedKeys.push(groupKey)
          const groupRunnables = hostRunnables.filter(r => r.group === group)

          return {
            key: groupKey,
            title: (
              <GroupTitle
                group={group}
                count={groupRunnables.length}
                active={activeScope.includes(group)}
                onToggle={() => onScopeToggle(group)}
              />
            ),
            children: groupRunnables.map(r => {
              const runnableKey = `${host.name}/${group}/${r.name}`
              expandedKeys.push(runnableKey)
              return {
                key: runnableKey,
                title: <RunnableTitle runnable={r} />,
                children: r.args.map(arg => ({
                  key: `${runnableKey}/${arg.name}`,
                  title: <ArgTitle arg={arg} />,
                  isLeaf: true,
                })),
              }
            }),
          }
        }),
      }
    })

    return { treeData: data, defaultExpandedKeys: expandedKeys }
  }, [hosts, runnables, selectedHost, search, activeScope, onScopeToggle])

  return (
    <div>
      <Input
        prefix={<SearchOutlined />}
        placeholder="Search runnables, groups, args…"
        value={search}
        onChange={e => setSearch(e.target.value)}
        allowClear
        style={{ marginBottom: 16, maxWidth: 360 }}
      />
      <Tree
        treeData={treeData}
        expandedKeys={defaultExpandedKeys}
        selectable={false}
        blockNode={false}
        style={{ background: 'transparent', fontFamily: 'monospace' }}
      />
    </div>
  )
}

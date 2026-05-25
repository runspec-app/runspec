import { Card, Badge, Tag, Typography, Space } from 'antd'
import type { Host } from '../bridge'

const { Title, Text } = Typography

interface HostsViewProps {
  hosts: Host[]
  activeScope: string[]
  onScopeToggle: (group: string) => void
}

export function HostsView({ hosts, activeScope, onScopeToggle }: HostsViewProps) {

  return (
    <div>
      <Title level={4} style={{ marginBottom: 16 }}>Hosts</Title>
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        {hosts.map(h => (
          <Card key={h.name} size="small">
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
              <Badge status={h.connected ? 'success' : 'error'} />
              <Text strong style={{ fontFamily: 'monospace' }}>{h.name}</Text>
              {h.connected ? (
                <>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {h.runnableCount} runnables
                  </Text>
                  {h.groups.length > 0 && (
                    <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {h.groups.length === 1 ? '1 group:' : `${h.groups.length} groups:`}
                      </Text>
                      {h.groups.map(g => {
                        const active = activeScope.includes(g)
                        return (
                          <Tag
                            key={g}
                            color={active ? 'geekblue' : 'blue'}
                            onClick={() => onScopeToggle(g)}
                            style={{ cursor: 'pointer', fontWeight: active ? 600 : 400, margin: 0 }}
                            title={active ? 'Remove from scope' : 'Add to scope'}
                          >
                            {g}
                          </Tag>
                        )
                      })}
                    </span>
                  )}
                </>
              ) : (
                <Text type="secondary" style={{ fontSize: 12 }}>disconnected</Text>
              )}
            </div>
          </Card>
        ))}
      </Space>
    </div>
  )
}

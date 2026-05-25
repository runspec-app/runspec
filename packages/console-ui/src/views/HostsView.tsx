import { useEffect, useState } from 'react'
import { Card, Badge, Typography, Space } from 'antd'
import { bridge, type Host } from '../bridge'

const { Title, Text } = Typography

export function HostsView() {
  const [hosts, setHosts] = useState<Host[]>([])

  useEffect(() => {
    bridge.get_hosts().then(setHosts)
  }, [])

  return (
    <div>
      <Title level={4} style={{ marginBottom: 16 }}>Hosts</Title>
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        {hosts.map(h => (
          <Card key={h.name} size="small">
            <Space>
              <Badge status={h.connected ? 'success' : 'error'} />
              <Text strong style={{ fontFamily: 'monospace' }}>{h.name}</Text>
              <Text type="secondary">{h.connected ? `${h.runnableCount} runnables` : 'disconnected'}</Text>
            </Space>
          </Card>
        ))}
      </Space>
    </div>
  )
}

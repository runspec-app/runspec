import { useState } from 'react'
import { ConfigProvider, Layout, Menu, Badge, theme } from 'antd'
import {
  ThunderboltOutlined,
  AppstoreOutlined,
  ClusterOutlined,
  HistoryOutlined,
  CalendarOutlined,
} from '@ant-design/icons'
import { ConsoleView } from './views/ConsoleView'
import { RunnablesView } from './views/RunnablesView'
import { HostsView } from './views/HostsView'
import { HistoryView } from './views/HistoryView'
import { SchedulesView } from './views/SchedulesView'
import { useInFlight } from './bridge/useInFlight'
import type { InFlightRecord, HistoryRecord } from './bridge'

const { Sider, Content } = Layout

type ViewKey = 'console' | 'runnables' | 'hosts' | 'history' | 'schedules'

function makeViews(
  inFlight: InFlightRecord[],
  onHistoryRerun: (record: HistoryRecord) => void,
): Record<ViewKey, React.ReactNode> {
  return {
    console: <ConsoleView inFlight={inFlight} />,
    runnables: <RunnablesView />,
    hosts: <HostsView />,
    history: <HistoryView onRerun={onHistoryRerun} />,
    schedules: <SchedulesView />,
  }
}

export default function App() {
  const [view, setView] = useState<ViewKey>('console')
  const inFlight = useInFlight()

  const handleHistoryRerun = (record: HistoryRecord) => {
    setView('console')
    window.dispatchEvent(new CustomEvent('runspec:rerun', {
      detail: { host: record.host, runnable: record.runnable, args: record.args },
    }))
  }

  const navItems = [
    {
      key: 'console',
      icon: <ThunderboltOutlined />,
      label: inFlight.length > 0
        ? <Badge count={inFlight.length} size="small" offset={[6, 0]}>Console</Badge>
        : 'Console',
    },
    { key: 'runnables', icon: <AppstoreOutlined />, label: 'Runnables' },
    { key: 'hosts',     icon: <ClusterOutlined />,  label: 'Hosts' },
    { key: 'history',   icon: <HistoryOutlined />,  label: 'History' },
    { key: 'schedules', icon: <CalendarOutlined />, label: 'Schedules' },
  ]

  return (
    <ConfigProvider theme={{ algorithm: theme.darkAlgorithm }}>
      <Layout style={{ height: '100vh', background: '#0d0d0d' }}>
        <Sider width={200} style={{ background: '#111', borderRight: '1px solid #222' }}>
          <div style={{
            padding: '16px 20px 12px',
            fontFamily: 'monospace', fontWeight: 700,
            fontSize: 15, color: '#4fc1ff',
            borderBottom: '1px solid #222',
          }}>
            runspec console
          </div>
          <Menu
            mode="inline"
            selectedKeys={[view]}
            onClick={({ key }) => setView(key as ViewKey)}
            items={navItems}
            style={{ background: 'transparent', border: 'none', marginTop: 8 }}
          />
        </Sider>
        <Content style={{
          background: '#111', padding: 24,
          display: 'flex', flexDirection: 'column',
          overflow: 'hidden',
        }}>
          {makeViews(inFlight, handleHistoryRerun)[view]}
        </Content>
      </Layout>
    </ConfigProvider>
  )
}

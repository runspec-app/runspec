import { useState } from 'react'
import { ConfigProvider, Layout, Menu, theme } from 'antd'
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

const { Sider, Content } = Layout

type ViewKey = 'console' | 'runnables' | 'hosts' | 'history' | 'schedules'

const VIEWS: Record<ViewKey, React.ReactNode> = {
  console: <ConsoleView />,
  runnables: <RunnablesView />,
  hosts: <HostsView />,
  history: <HistoryView />,
  schedules: <SchedulesView />,
}

const NAV_ITEMS = [
  { key: 'console', icon: <ThunderboltOutlined />, label: 'Console' },
  { key: 'runnables', icon: <AppstoreOutlined />, label: 'Runnables' },
  { key: 'hosts', icon: <ClusterOutlined />, label: 'Hosts' },
  { key: 'history', icon: <HistoryOutlined />, label: 'History' },
  { key: 'schedules', icon: <CalendarOutlined />, label: 'Schedules' },
]

export default function App() {
  const [view, setView] = useState<ViewKey>('console')

  return (
    <ConfigProvider theme={{ algorithm: theme.darkAlgorithm }}>
      <Layout style={{ height: '100vh', background: '#0d0d0d' }}>
        <Sider
          width={200}
          style={{ background: '#111', borderRight: '1px solid #222' }}
        >
          <div style={{
            padding: '16px 20px 12px',
            fontFamily: 'monospace', fontWeight: 700,
            fontSize: 15, color: '#4fc1ff',
            borderBottom: '1px solid #222',
          }}>
            runspec
          </div>
          <Menu
            mode="inline"
            selectedKeys={[view]}
            onClick={({ key }) => setView(key as ViewKey)}
            items={NAV_ITEMS}
            style={{ background: 'transparent', border: 'none', marginTop: 8 }}
          />
        </Sider>
        <Content style={{
          background: '#111', padding: 24,
          display: 'flex', flexDirection: 'column',
          overflow: 'hidden',
        }}>
          {VIEWS[view]}
        </Content>
      </Layout>
    </ConfigProvider>
  )
}

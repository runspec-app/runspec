import { useState } from 'react'
import { ConfigProvider, Layout, Menu, Badge, theme, Button, Tooltip } from 'antd'
import {
  ThunderboltOutlined,
  AppstoreOutlined,
  ClusterOutlined,
  HistoryOutlined,
  CalendarOutlined,
  SunOutlined,
  MoonOutlined,
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

export default function App() {
  const [view, setView] = useState<ViewKey>('console')
  const [collapsed, setCollapsed] = useState(false)
  const [isDark, setIsDark] = useState(() => localStorage.getItem('theme') !== 'light')
  const [pendingChat, setPendingChat] = useState<string | null>(null)
  const inFlight = useInFlight()

  const toggleTheme = () => {
    const next = !isDark
    setIsDark(next)
    localStorage.setItem('theme', next ? 'dark' : 'light')
  }

  const handleHistoryRerun = (record: HistoryRecord) => {
    setView('console')
    window.dispatchEvent(new CustomEvent('runspec:rerun', {
      detail: { host: record.host, runnable: record.runnable, args: record.args },
    }))
  }

  const handleAskLlm = (text: string) => {
    setView('console')
    setPendingChat(text)
  }

  const siderBg   = isDark ? '#111'   : '#fafafa'
  const borderCol = isDark ? '#222'   : '#e0e0e0'
  const titleCol  = isDark ? '#4fc1ff': '#0958d9'
  const contentBg = isDark ? '#111'   : '#ffffff'

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
    <ConfigProvider theme={{ algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
      <Layout style={{ height: '100vh', background: contentBg }}>
        <Sider
          width={200}
          collapsedWidth={56}
          collapsed={collapsed}
          onCollapse={setCollapsed}
          collapsible
          style={{ background: siderBg, borderRight: `1px solid ${borderCol}` }}
        >
          <div style={{
            display: 'flex', alignItems: 'center',
            justifyContent: collapsed ? 'center' : 'space-between',
            padding: collapsed ? '14px 0' : '14px 12px 12px 20px',
            borderBottom: `1px solid ${borderCol}`,
            minHeight: 48,
          }}>
            {!collapsed && (
              <span style={{
                fontFamily: 'monospace', fontWeight: 700,
                fontSize: 15, color: titleCol,
              }}>
                runspec console
              </span>
            )}
            <Tooltip title={isDark ? 'Light theme' : 'Dark theme'} placement="right">
              <Button
                type="text"
                size="small"
                icon={isDark ? <SunOutlined /> : <MoonOutlined />}
                onClick={toggleTheme}
                style={{ color: titleCol, flexShrink: 0 }}
              />
            </Tooltip>
          </div>
          <Menu
            mode="inline"
            inlineCollapsed={collapsed}
            selectedKeys={[view]}
            onClick={({ key }) => setView(key as ViewKey)}
            items={navItems}
            style={{ background: 'transparent', border: 'none', marginTop: 8 }}
          />
        </Sider>
        <Content style={{
          background: contentBg, padding: 24,
          display: 'flex', flexDirection: 'column',
          overflow: 'hidden',
        }}>
          {view === 'console' && (
            <ConsoleView
              inFlight={inFlight}
              pendingChat={pendingChat}
              onChatSent={() => setPendingChat(null)}
            />
          )}
          {view === 'runnables'  && <RunnablesView />}
          {view === 'hosts'      && <HostsView />}
          {view === 'history'    && <HistoryView onRerun={handleHistoryRerun} onAskLlm={handleAskLlm} />}
          {view === 'schedules'  && <SchedulesView />}
        </Content>
      </Layout>
    </ConfigProvider>
  )
}

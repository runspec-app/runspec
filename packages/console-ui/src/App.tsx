import { useEffect, useState } from 'react'
import { ConfigProvider, Layout, Menu, Badge, theme, Button, Tooltip, Popover, Checkbox, Segmented } from 'antd'
import {
  ThunderboltOutlined,
  AppstoreOutlined,
  ClusterOutlined,
  HistoryOutlined,
  CalendarOutlined,
  SunOutlined,
  MoonOutlined,
  SettingOutlined,
  FilterOutlined,
} from '@ant-design/icons'
import { ConsoleView } from './views/ConsoleView'
import { RunnablesView } from './views/RunnablesView'
import { HostsView } from './views/HostsView'
import { HistoryView } from './views/HistoryView'
import { SchedulesView } from './views/SchedulesView'
import { CommandInput } from './components/CommandInput'
import { SettingsDrawer } from './components/SettingsDrawer'
import { useInFlight } from './bridge/useInFlight'
import { bridge, type HistoryRecord, type Host, type Runnable } from './bridge'
import { ThemeContext } from './ThemeContext'

const { Header, Sider, Content } = Layout

type ViewKey = 'console' | 'runnables' | 'hosts' | 'history' | 'schedules'

export default function App() {
  const [view, setView] = useState<ViewKey>('console')
  const [collapsed, setCollapsed] = useState(false)
  const [isDark, setIsDark] = useState(() => localStorage.getItem('theme') !== 'light')
  const [autoSwitch, setAutoSwitch] = useState(() => localStorage.getItem('autoSwitch') !== 'false')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [runnables, setRunnables] = useState<Runnable[]>([])
  const [hosts, setHosts] = useState<Host[]>([])
  const [hostRole, setHostRole] = useState<'all' | 'primary' | 'secondary'>(
    () => (localStorage.getItem('hostRole') as 'all' | 'primary' | 'secondary') ?? 'all'
  )
  const [inputHistory, setInputHistory] = useState<string[]>([])
  const [pendingChat, setPendingChat] = useState<string | null>(null)
  const [historySearch, setHistorySearch] = useState('')
  const [activeScope, setActiveScope] = useState<string[]>([])
  const [scopePopoverOpen, setScopePopoverOpen] = useState(false)
  const inFlight = useInFlight()

  // Hosts with no role (local machine) are always included regardless of role filter
  const activeHostNames = new Set(
    hosts
      .filter(h => h.connected && (h.role === undefined || hostRole === 'all' || h.role === hostRole))
      .map(h => h.name)
  )
  const roleFilteredRunnables = runnables.filter(r => activeHostNames.has(r.host))
  const allGroups = [...new Set(roleFilteredRunnables.map(r => r.group))].sort()

  const handleScopeToggle = (group: string) => {
    setActiveScope(prev =>
      prev.includes(group) ? prev.filter(g => g !== group) : [...prev, group]
    )
  }

  useEffect(() => {
    bridge.get_runnables('local').then(setRunnables)
    bridge.get_hosts().then(setHosts)
  }, [])

  const toggleHostRole = (role: 'all' | 'primary' | 'secondary') => {
    setHostRole(role)
    localStorage.setItem('hostRole', role)
  }

  const toggleTheme = () => {
    const next = !isDark
    setIsDark(next)
    localStorage.setItem('theme', next ? 'dark' : 'light')
  }

  const toggleAutoSwitch = () => {
    const next = !autoSwitch
    setAutoSwitch(next)
    localStorage.setItem('autoSwitch', next ? 'true' : 'false')
  }

  const handleHistoryRerun = (record: HistoryRecord) => {
    setHistorySearch(record.runnable)
    setView('console')
    window.dispatchEvent(new CustomEvent('runspec:rerun', {
      detail: { host: record.host, runnable: record.runnable, args: record.args },
    }))
  }

  const handleAskLlm = (text: string) => {
    setView('console')
    setPendingChat(text)
  }

  const handleRunRunnable = (runnable: Runnable, args: Record<string, unknown>) => {
    setInputHistory(h => [...h, `/${runnable.name}`])
    if (autoSwitch) setView('console')
    window.dispatchEvent(new CustomEvent('runspec:invoke_runnable', { detail: { runnable, args } }))
  }

  const handleSendChat = (message: string) => {
    setInputHistory(h => [...h, message])
    if (autoSwitch) setView('console')
    window.dispatchEvent(new CustomEvent('runspec:send_chat', { detail: { message } }))
  }

  const headerBg  = isDark ? '#0d0d0d' : '#fafafa'
  const siderBg   = isDark ? '#111'    : '#fafafa'
  const borderCol = isDark ? '#222'    : '#e0e0e0'
  const titleCol  = isDark ? '#4fc1ff' : '#0958d9'
  const contentBg = isDark ? '#111'    : '#ffffff'
  const iconCol   = isDark ? '#888'    : '#999'

  const navItems = [
    {
      key: 'console',
      icon: inFlight.length > 0
        ? collapsed
          ? <ThunderboltOutlined style={{ color: '#ff4d4f' }} />
          : <Badge count={inFlight.length} size="small" offset={[2, -1]}><ThunderboltOutlined /></Badge>
        : <ThunderboltOutlined />,
      title: 'Console',
      label: 'Console',
    },
    { key: 'runnables', icon: <AppstoreOutlined />, title: 'Runnables', label: 'Runnables' },
    { key: 'hosts',     icon: <ClusterOutlined />,  title: 'Hosts',     label: 'Hosts' },
    { key: 'history',   icon: <HistoryOutlined />,  title: 'History',   label: 'History' },
    { key: 'schedules', icon: <CalendarOutlined />, title: 'Schedules', label: 'Schedules' },
  ]

  return (
    <ThemeContext.Provider value={isDark}>
      <ConfigProvider theme={{ algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
        <Layout style={{ height: '100vh', background: contentBg }}>

          {/* Top header: branding left, actions right */}
          <Header style={{
            height: 48, lineHeight: '48px', padding: '0 16px',
            background: headerBg, borderBottom: `1px solid ${borderCol}`,
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          }}>
            <span style={{ fontFamily: 'monospace', fontWeight: 700, fontSize: 15, color: titleCol }}>
              runspec console
            </span>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <Segmented
                size="small"
                value={hostRole}
                onChange={v => toggleHostRole(v as 'all' | 'primary' | 'secondary')}
                options={[
                  { label: 'All', value: 'all' },
                  { label: 'Primary', value: 'primary' },
                  { label: 'Secondary', value: 'secondary' },
                ]}
                style={{ fontSize: 11 }}
              />
              <Tooltip title={isDark ? 'Light theme' : 'Dark theme'}>
                <Button
                  type="text" size="small"
                  icon={isDark ? <SunOutlined /> : <MoonOutlined />}
                  onClick={toggleTheme}
                  style={{ color: iconCol }}
                />
              </Tooltip>
              <Tooltip title="Settings">
                <Button
                  type="text" size="small"
                  icon={<SettingOutlined />}
                  onClick={() => setSettingsOpen(true)}
                  style={{ color: iconCol }}
                />
              </Tooltip>
            </div>
          </Header>

          {/* Sidebar + content row */}
          <Layout style={{ flex: 1, overflow: 'hidden', background: contentBg }}>
            <Sider
              width={200}
              collapsedWidth={56}
              collapsed={collapsed}
              onCollapse={setCollapsed}
              collapsible
              style={{ background: siderBg, borderRight: `1px solid ${borderCol}` }}
            >
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
              background: contentBg, padding: 0,
              display: 'flex', flexDirection: 'column', overflow: 'hidden',
            }}>
              {/* View area */}
              <div style={{ flex: 1, padding: '24px 24px 16px', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
                {/* ConsoleView always mounted so blocks + listeners persist */}
                <div style={{ display: view === 'console' ? 'flex' : 'none', flexDirection: 'column', flex: 1, minHeight: 0 }}>
                  <ConsoleView
                    inFlight={inFlight}
                    pendingChat={pendingChat}
                    onChatSent={() => setPendingChat(null)}
                  />
                </div>
                {view !== 'console' && (
                  <div style={{ flex: 1, overflow: 'auto' }}>
                    {view === 'runnables' && <RunnablesView runnables={roleFilteredRunnables} activeScope={activeScope} onScopeToggle={handleScopeToggle} />}
                    {view === 'hosts'     && <HostsView hosts={hosts} activeScope={activeScope} onScopeToggle={handleScopeToggle} />}
                    {view === 'history'   && <HistoryView search={historySearch} onSearchChange={setHistorySearch} onRerun={handleHistoryRerun} onAskLlm={handleAskLlm} activeScope={activeScope} onScopeToggle={handleScopeToggle} />}
                    {view === 'schedules' && <SchedulesView />}
                  </div>
                )}
              </div>

              {/* Persistent command bar */}
              <div style={{
                borderTop: `1px solid ${borderCol}`,
                padding: '8px 12px 8px 16px',
                display: 'flex', alignItems: 'flex-end', gap: 8,
                background: contentBg,
              }}>
                <div style={{ flex: 1 }}>
                  <CommandInput
                    runnables={activeScope.length > 0 ? roleFilteredRunnables.filter(r => activeScope.includes(r.group)) : roleFilteredRunnables}
                    allGroups={allGroups}
                    activeScope={activeScope}
                    onScopeChange={setActiveScope}
                    onRunRunnable={handleRunRunnable}
                    onSendChat={handleSendChat}
                    history={inputHistory}
                  />
                </div>
                <Popover
                  open={scopePopoverOpen}
                  onOpenChange={setScopePopoverOpen}
                  trigger="click"
                  placement="topRight"
                  content={
                    <div style={{ minWidth: 180 }}>
                      {allGroups.length === 0
                        ? <span style={{ color: '#888', fontSize: 12 }}>No groups available</span>
                        : allGroups.map(g => (
                          <div key={g} style={{ padding: '5px 0' }}>
                            <Checkbox
                              checked={activeScope.includes(g)}
                              onChange={() => handleScopeToggle(g)}
                            >
                              <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{g}</span>
                            </Checkbox>
                          </div>
                        ))
                      }
                      {activeScope.length > 0 && (
                        <div style={{ borderTop: '1px solid #333', marginTop: 6, paddingTop: 6 }}>
                          <Button size="small" type="text" onClick={() => { setActiveScope([]); setScopePopoverOpen(false) }}
                            style={{ padding: 0, fontSize: 12, color: '#888' }}>
                            Clear all
                          </Button>
                        </div>
                      )}
                    </div>
                  }
                >
                  <Tooltip title="Scope to groups" placement="top">
                    <Badge count={activeScope.length} size="small" offset={[2, -2]}>
                      <Button
                        type="text" size="small"
                        icon={<FilterOutlined />}
                        style={{
                          color: activeScope.length > 0 ? titleCol : iconCol,
                          background: activeScope.length > 0
                            ? (isDark ? 'rgba(79,193,255,0.08)' : 'rgba(9,88,217,0.06)')
                            : 'transparent',
                          marginBottom: 6, borderRadius: 6,
                        }}
                      />
                    </Badge>
                  </Tooltip>
                </Popover>
                <Tooltip
                  title={autoSwitch
                    ? 'Auto-switch to Console: on — click to stay on current view'
                    : 'Auto-switch to Console: off — click to enable'}
                  placement="top"
                >
                  <Button
                    type="text" size="small"
                    icon={<ThunderboltOutlined />}
                    onClick={toggleAutoSwitch}
                    style={{
                      color: autoSwitch ? titleCol : (isDark ? '#444' : '#bbb'),
                      background: autoSwitch
                        ? (isDark ? 'rgba(79,193,255,0.08)' : 'rgba(9,88,217,0.06)')
                        : 'transparent',
                      marginBottom: 6, borderRadius: 6,
                    }}
                  />
                </Tooltip>
              </div>
            </Content>
          </Layout>
        </Layout>

        <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      </ConfigProvider>
    </ThemeContext.Provider>
  )
}

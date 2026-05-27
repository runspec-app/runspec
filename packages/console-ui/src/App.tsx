import { useEffect, useState } from 'react'
import { ConfigProvider, Badge, theme, Button, Tooltip, Tag } from 'antd'
import {
  ThunderboltOutlined,
  AppstoreOutlined,
  FormOutlined,
  HistoryOutlined,
  CalendarOutlined,
  SunOutlined,
  MoonOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import { ConsoleView } from './views/ConsoleView'
import { SpecsView } from './views/SpecsView'
import { HistoryView } from './views/HistoryView'
import { SchedulesView } from './views/SchedulesView'
import { FormsView, type PendingForm } from './views/FormsView'
import { CommandInput } from './components/CommandInput'
import { SettingsDrawer } from './components/SettingsDrawer'
import { useInFlight } from './bridge/useInFlight'
import { bridge, type HistoryRecord, type Host, type Runnable } from './bridge'
import { ThemeContext } from './ThemeContext'

type ViewKey = 'console' | 'specs' | 'history' | 'forms' | 'schedules'

export default function App() {
  const [view, setView] = useState<ViewKey>('console')
  const [isDark, setIsDark] = useState(() => localStorage.getItem('theme') !== 'light')
  const [autoSwitch, setAutoSwitch] = useState(() => localStorage.getItem('autoSwitch') !== 'false')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [runnables, setRunnables] = useState<Runnable[]>([])
  const [hosts, setHosts] = useState<Host[]>([])
  const [selectedHost, setSelectedHost] = useState<string>('')
  const [inputHistory, setInputHistory] = useState<string[]>([])
  const [pendingChat, setPendingChat] = useState<string | null>(null)
  const [pendingForm, setPendingForm] = useState<PendingForm | null>(null)
  const [historySearch, setHistorySearch] = useState('')
  const [activeScope, setActiveScope] = useState<string[]>([])
  const inFlight = useInFlight()

  const selectedHostObj = hosts.find(h => h.name === selectedHost)
  const selectedHostGroups = selectedHostObj?.groups ?? []
  const hostFilteredRunnables = runnables.filter(r => r.host === selectedHost)
  const scopedRunnables = activeScope.length > 0
    ? hostFilteredRunnables.filter(r => activeScope.includes(r.group))
    : hostFilteredRunnables

  const handleScopeToggle = (group: string) => {
    setActiveScope(prev =>
      prev.includes(group) ? prev.filter(g => g !== group) : [...prev, group]
    )
  }

  const handleHostSelect = (name: string) => {
    setSelectedHost(name)
    setActiveScope([])
  }

  useEffect(() => {
    bridge.get_runnables('all').then(setRunnables)
    bridge.get_hosts().then(hs => {
      setHosts(hs)
      setSelectedHost(hs[0]?.name ?? '')
    })
    // Re-fetch when the background refresh cycle completes
    const onHostsUpdated = () => bridge.get_hosts().then(setHosts)
    const onRunnablesUpdated = () => bridge.get_runnables('all').then(setRunnables)
    window.addEventListener('runspec:hosts_updated', onHostsUpdated)
    window.addEventListener('runspec:runnables_updated', onRunnablesUpdated)
    return () => {
      window.removeEventListener('runspec:hosts_updated', onHostsUpdated)
      window.removeEventListener('runspec:runnables_updated', onRunnablesUpdated)
    }
  }, [])

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

  const handleRunRunnable = (runnable: Runnable, args: Record<string, unknown>, commandPath: string[] = []) => {
    const cmd = commandPath.length > 0 ? `${runnable.name} ${commandPath.join(' ')}` : runnable.name
    const argStr = Object.entries(args).map(([k, v]) => v === true ? `--${k}` : `--${k}=${v}`).join(' ')
    const label = argStr ? `/${cmd} ${argStr}` : `/${cmd}`
    setInputHistory(h => [...h, label])
    setView('console')
    window.dispatchEvent(new CustomEvent('runspec:invoke_runnable', { detail: { runnable, args, commandPath } }))
  }

  const handleOpenForm = (runnable: Runnable, commandPath: string[] = []) => {
    setPendingForm({ runnable, commandPath })
    setView('forms')
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
  const textCol   = isDark ? '#ccc'    : '#333'

  const navTabs = [
    {
      key: 'console' as ViewKey,
      label: 'Console',
      icon: inFlight.length > 0
        ? <Badge count={inFlight.length} size="small" offset={[2, -1]}><ThunderboltOutlined /></Badge>
        : <ThunderboltOutlined />,
    },
    { key: 'history'   as ViewKey, label: 'History',   icon: <HistoryOutlined /> },
    { key: 'specs'     as ViewKey, label: 'Specs',     icon: <AppstoreOutlined /> },
    { key: 'forms'     as ViewKey, label: 'Forms',     icon: <FormOutlined /> },
    { key: 'schedules' as ViewKey, label: 'Schedules', icon: <CalendarOutlined /> },
  ]

  return (
    <ThemeContext.Provider value={isDark}>
      <ConfigProvider theme={{
          algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
          token: { fontFamily: 'monospace' },
        }}>
        <div style={{ height: '100vh', display: 'flex', overflow: 'hidden', background: contentBg, fontFamily: 'monospace' }}>

          {/* Fixed host sidebar */}
          <div style={{
            width: 180, flexShrink: 0,
            background: siderBg, borderRight: `1px solid ${borderCol}`,
            display: 'flex', flexDirection: 'column',
          }}>
            <div style={{
              padding: '13px 16px 12px',
              fontWeight: 700, fontSize: 14, color: titleCol,
              borderBottom: `1px solid ${borderCol}`, flexShrink: 0,
            }}>
              runspec
            </div>
            <div style={{ flex: 1, overflowY: 'auto', paddingTop: 4 }}>
              {(() => {
                const hostRow = (h: typeof hosts[0]) => (
                  <div
                    key={h.name}
                    onClick={() => handleHostSelect(h.name)}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 8,
                      padding: '8px 12px 8px 14px', cursor: 'pointer',
                      borderLeft: selectedHost === h.name
                        ? `2px solid ${titleCol}`
                        : '2px solid transparent',
                      background: selectedHost === h.name
                        ? (isDark ? 'rgba(79,193,255,0.08)' : 'rgba(9,88,217,0.06)')
                        : 'transparent',
                    }}
                  >
                    <span style={{ fontSize: 9, lineHeight: 1, color: h.connected ? '#52c41a' : '#595959' }}>●</span>
                    <span style={{
                      fontSize: 13, color: textCol,
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>{h.name}</span>
                  </div>
                )

                // Group all hosts — ungrouped (incl. local) → 'Hosts' heading
                const groupMap = new Map<string, typeof hosts>()
                const groupOrder: string[] = []
                for (const h of hosts) {
                  const g = h.group ?? ''
                  if (!groupMap.has(g)) { groupMap.set(g, []); groupOrder.push(g) }
                  groupMap.get(g)!.push(h)
                }
                // Ungrouped ('') first, named groups alphabetically after
                const sortedGroupKeys = [...groupOrder].sort((a, b) => {
                  if (!a && b) return -1
                  if (a && !b) return 1
                  return a.localeCompare(b)
                })

                return (
                  <>
                    {sortedGroupKeys.map(g => (
                      <div key={g || '__hosts__'}>
                        <div style={{
                          padding: '10px 14px 3px',
                          fontSize: 10, color: isDark ? '#aaa' : '#999',
                          textTransform: 'uppercase', letterSpacing: '0.08em',
                          userSelect: 'none',
                        }}>
                          {g || 'Hosts'}
                        </div>
                        {groupMap.get(g)!.map(hostRow)}
                      </div>
                    ))}
                  </>
                )
              })()}
            </div>
          </div>

          {/* Content column */}
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>

            {/* Tab bar */}
            <div style={{
              height: 44, flexShrink: 0,
              background: headerBg, borderBottom: `1px solid ${borderCol}`,
              display: 'flex', alignItems: 'stretch',
            }}>
              {navTabs.map(tab => (
                <button
                  key={tab.key}
                  onClick={() => setView(tab.key)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '0 16px', border: 'none', background: 'transparent',
                    cursor: 'pointer', fontSize: 13,
                    color: view === tab.key ? titleCol : iconCol,
                    borderBottom: view === tab.key ? `2px solid ${titleCol}` : '2px solid transparent',
                    transition: 'color 0.15s',
                  }}
                >
                  {tab.icon}
                  {tab.label}
                </button>
              ))}
              <div style={{ flex: 1 }} />
              <div style={{ display: 'flex', alignItems: 'center', gap: 2, paddingRight: 10 }}>
                <Tooltip title={isDark ? 'Light theme' : 'Dark theme'}>
                  <Button type="text" size="small" icon={isDark ? <SunOutlined /> : <MoonOutlined />} onClick={toggleTheme} style={{ color: iconCol }} />
                </Tooltip>
                <Tooltip title="Settings">
                  <Button type="text" size="small" icon={<SettingOutlined />} onClick={() => setSettingsOpen(true)} style={{ color: iconCol }} />
                </Tooltip>
              </div>
            </div>

            {/* View area */}
            <div style={{ flex: 1, padding: '24px 24px 16px', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
              <div style={{ display: view === 'console' ? 'flex' : 'none', flexDirection: 'column', flex: 1, minHeight: 0 }}>
                <ConsoleView inFlight={inFlight} pendingChat={pendingChat} onChatSent={() => setPendingChat(null)} />
              </div>
              {view !== 'console' && (
                <div style={{ flex: 1, overflow: 'auto' }}>
                  {view === 'specs'     && <SpecsView runnables={runnables} selectedHost={selectedHost} activeScope={activeScope} onScopeToggle={handleScopeToggle} />}
                  {view === 'history'   && <HistoryView search={historySearch} onSearchChange={setHistorySearch} onRerun={handleHistoryRerun} onAskLlm={handleAskLlm} activeScope={activeScope} onScopeToggle={handleScopeToggle} selectedHost={selectedHost} />}
                  {view === 'forms'     && <FormsView runnables={runnables} hosts={hosts} selectedHost={selectedHost} activeScope={activeScope} onRunRunnable={handleRunRunnable} pendingForm={pendingForm} onPendingFormClear={() => setPendingForm(null)} />}
                  {view === 'schedules' && <SchedulesView hosts={hosts} runnables={runnables} selectedHost={selectedHost} />}
                </div>
              )}
            </div>

            {/* Group strip */}
            {selectedHostGroups.length > 0 && (
              <div style={{
                borderTop: `1px solid ${borderCol}`,
                padding: '5px 16px',
                display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap',
                background: headerBg, flexShrink: 0,
              }}>
                {selectedHostGroups.map(g => (
                  <Tag
                    key={g}
                    onClick={() => handleScopeToggle(g)}
                    closable={activeScope.includes(g)}
                    onClose={e => { e.preventDefault(); handleScopeToggle(g) }}
                    color={activeScope.includes(g) ? 'blue' : undefined}
                    style={{ cursor: 'pointer', fontSize: 12, margin: 0, userSelect: 'none' }}
                  >
                    {g}
                  </Tag>
                ))}
                {activeScope.length > 0 && (
                  <Button
                    size="small" type="text"
                    onClick={() => setActiveScope([])}
                    style={{ fontSize: 11, color: '#888', padding: '0 4px', height: 'auto' }}
                  >
                    clear
                  </Button>
                )}
              </div>
            )}

            {/* Command bar */}
            <div style={{
              borderTop: `1px solid ${borderCol}`,
              padding: '8px 12px 8px 16px',
              background: contentBg, flexShrink: 0,
            }}>
              <CommandInput
                runnables={scopedRunnables}
                onRunRunnable={handleRunRunnable}
                onOpenForm={handleOpenForm}
                onSendChat={handleSendChat}
                history={inputHistory}
                autoSwitch={autoSwitch}
                onToggleAutoSwitch={toggleAutoSwitch}
              />
            </div>

          </div>
        </div>

        <SettingsDrawer
          open={settingsOpen}
          onClose={() => setSettingsOpen(false)}
          onHostsChanged={() => {
            bridge.get_hosts().then(setHosts)
            bridge.get_runnables('all').then(setRunnables)
          }}
        />
      </ConfigProvider>
    </ThemeContext.Provider>
  )
}

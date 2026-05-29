import React, { useEffect, useState } from 'react'
import { ConfigProvider, Badge, theme, Button, Tooltip, Tag, Dropdown } from 'antd'
import {
  ThunderboltOutlined,
  AppstoreOutlined,
  FormOutlined,
  HistoryOutlined,
  CalendarOutlined,
  SunOutlined,
  MoonOutlined,
  SettingOutlined,
  CodeOutlined,
} from '@ant-design/icons'
import { ConsoleView } from './views/ConsoleView'
import { SpecsView } from './views/SpecsView'
import { HistoryView } from './views/HistoryView'
import { SchedulesView } from './views/SchedulesView'
import { FormsView, type PendingForm } from './views/FormsView'
import { CommandInput } from './components/CommandInput'
import { SettingsDrawer } from './components/SettingsDrawer'
import { TerminalTab } from './components/TerminalTab'
import { useInFlight } from './bridge/useInFlight'
import { bridge, type HistoryRecord, type Host, type Runnable } from './bridge'
import { ThemeContext } from './ThemeContext'

type ViewKey = 'console' | 'specs' | 'history' | 'forms' | 'schedules'

interface TerminalSession {
  id: string
  host: string
}

export default function App() {
  const [view, setView] = useState<ViewKey>('console')
  const [isDark, setIsDark] = useState(() => localStorage.getItem('theme') !== 'light')
  const [autoSwitch, setAutoSwitch] = useState(() => localStorage.getItem('autoSwitch') !== 'false')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [sshKeyAgeDays, setSshKeyAgeDays] = useState<number | null>(null)
  const [runnables, setRunnables] = useState<Runnable[]>([])
  const [hosts, setHosts] = useState<Host[]>([])
  const [selectedHost, setSelectedHost] = useState<string>('')
  const [inputHistory, setInputHistory] = useState<string[]>([])
  const [pendingChat, setPendingChat] = useState<string | null>(null)
  const [pendingForm, setPendingForm] = useState<PendingForm | null>(null)
  const [historySearch, setHistorySearch] = useState('')
  const [activeScope, setActiveScope] = useState<string[]>([])
  const [terminals, setTerminals] = useState<TerminalSession[]>([])
  const [activeTerminalId, setActiveTerminalId] = useState<string | null>(null)
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

  const loadSshKeyAge = () => {
    bridge.get_config().then(cfg => {
      const ssh = (cfg.ssh ?? {}) as Record<string, string>
      const createdAt = ssh.key_created_at
      if (createdAt) {
        setSshKeyAgeDays(Math.floor((Date.now() - new Date(createdAt).getTime()) / (1000 * 60 * 60 * 24)))
      } else {
        setSshKeyAgeDays(null)
      }
    })
  }

  useEffect(() => { loadSshKeyAge() }, [])

  useEffect(() => {
    bridge.get_runnables('all').then(setRunnables)
    bridge.get_hosts().then(hs => {
      setHosts(hs)
      setSelectedHost(hs[0]?.name ?? '')
    })
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
    setActiveTerminalId(null)
    window.dispatchEvent(new CustomEvent('runspec:rerun', {
      detail: { host: record.host, runnable: record.runnable, args: record.args },
    }))
  }

  const handleAskLlm = (text: string) => {
    setView('console')
    setActiveTerminalId(null)
    setPendingChat(text)
  }

  const handleRunRunnable = (runnable: Runnable, args: Record<string, unknown>, commandPath: string[] = []) => {
    const cmd = commandPath.length > 0 ? `${runnable.name} ${commandPath.join(' ')}` : runnable.name
    const argStr = Object.entries(args).map(([k, v]) => v === true ? `--${k}` : `--${k}=${v}`).join(' ')
    const label = argStr ? `/${cmd} ${argStr}` : `/${cmd}`
    setInputHistory(h => [...h, label])
    setView('console')
    setActiveTerminalId(null)
    window.dispatchEvent(new CustomEvent('runspec:invoke_runnable', { detail: { runnable, args, commandPath } }))
  }

  const handleOpenForm = (runnable: Runnable, commandPath: string[] = []) => {
    setPendingForm({ runnable, commandPath })
    setView('forms')
    setActiveTerminalId(null)
  }

  const handleSendChat = (message: string) => {
    setInputHistory(h => [...h, message])
    if (autoSwitch) { setView('console'); setActiveTerminalId(null) }
    window.dispatchEvent(new CustomEvent('runspec:send_chat', { detail: { message } }))
  }

  const handleOpenTerminal = async (hostName: string) => {
    try {
      const sessionId = await bridge.open_terminal(hostName)
      setTerminals(prev => [...prev, { id: sessionId, host: hostName }])
      setActiveTerminalId(sessionId)
    } catch (err) {
      console.error('Failed to open terminal:', err)
    }
  }

  const handleCloseTerminal = (sessionId: string) => {
    bridge.close_terminal(sessionId).catch(console.error)
    setTerminals(prev => prev.filter(t => t.id !== sessionId))
    if (activeTerminalId === sessionId) {
      setActiveTerminalId(null)
    }
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
                  <Dropdown
                    key={h.name}
                    trigger={['contextMenu']}
                    menu={{
                      items: h.role !== undefined && h.connected ? [
                        {
                          key: 'open_terminal',
                          icon: <CodeOutlined />,
                          label: 'Open SSH terminal',
                          onClick: () => handleOpenTerminal(h.name),
                        },
                      ] : [],
                    }}
                  >
                    <div
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
                  </Dropdown>
                )

                const groupMap = new Map<string, typeof hosts>()
                const groupOrder: string[] = []
                for (const h of hosts) {
                  const g = h.group ?? ''
                  if (!groupMap.has(g)) { groupMap.set(g, []); groupOrder.push(g) }
                  groupMap.get(g)!.push(h)
                }
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
                  onClick={() => { setView(tab.key); setActiveTerminalId(null) }}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '0 16px', border: 'none', background: 'transparent',
                    cursor: 'pointer', fontSize: 13,
                    color: view === tab.key && activeTerminalId === null ? titleCol : iconCol,
                    borderBottom: view === tab.key && activeTerminalId === null ? `2px solid ${titleCol}` : '2px solid transparent',
                    transition: 'color 0.15s',
                  }}
                >
                  {tab.icon}
                  {tab.label}
                </button>
              ))}

              {terminals.length > 0 && (
                <div style={{
                  width: 1, background: borderCol, alignSelf: 'stretch', margin: '8px 2px', flexShrink: 0,
                }} />
              )}
              {terminals.map(t => (
                <div
                  key={t.id}
                  onClick={() => setActiveTerminalId(t.id)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 5,
                    padding: '0 6px 0 12px', cursor: 'pointer', userSelect: 'none',
                    color: activeTerminalId === t.id ? titleCol : iconCol,
                    borderBottom: activeTerminalId === t.id ? `2px solid ${titleCol}` : '2px solid transparent',
                    transition: 'color 0.15s',
                    fontSize: 13,
                  }}
                >
                  <CodeOutlined style={{ fontSize: 11 }} />
                  <span style={{
                    maxWidth: 100, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>{t.host}</span>
                  <button
                    onClick={e => { e.stopPropagation(); handleCloseTerminal(t.id) }}
                    title="Close terminal"
                    style={{
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      width: 18, height: 18, border: 'none', borderRadius: 3, background: 'transparent',
                      cursor: 'pointer', fontSize: 14, lineHeight: 1,
                      color: isDark ? '#555' : '#bbb',
                      padding: 0, marginLeft: 2,
                    }}
                    onMouseEnter={e => (e.currentTarget.style.color = isDark ? '#aaa' : '#666')}
                    onMouseLeave={e => (e.currentTarget.style.color = isDark ? '#555' : '#bbb')}
                  >
                    ×
                  </button>
                </div>
              ))}

              <div style={{ flex: 1 }} />

              <div style={{ display: 'flex', alignItems: 'center', gap: 2, padding: '0 8px', borderRight: `1px solid ${borderCol}` }}>
                {hosts.map(h => (
                  <button
                    key={h.name}
                    onClick={() => handleHostSelect(h.name)}
                    title={h.connected ? `${h.name} — connected` : `${h.name} — disconnected`}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 4,
                      padding: '2px 7px', border: 'none', borderRadius: 10, cursor: 'pointer',
                      background: selectedHost === h.name
                        ? (isDark ? 'rgba(79,193,255,0.1)' : 'rgba(9,88,217,0.08)')
                        : 'transparent',
                      color: h.connected ? (isDark ? '#bbb' : '#333') : (isDark ? '#444' : '#bbb'),
                      fontSize: 11, fontFamily: 'monospace',
                      transition: 'background 0.12s',
                    }}
                  >
                    <span style={{ fontSize: 8, lineHeight: 1, color: h.connected ? '#52c41a' : '#595959' }}>●</span>
                    {h.name}
                  </button>
                ))}
              </div>

              <div style={{ display: 'flex', alignItems: 'center', gap: 2, paddingRight: 8 }}>
                <Tooltip title={isDark ? 'Light theme' : 'Dark theme'}>
                  <Button type="text" size="small" icon={isDark ? <SunOutlined /> : <MoonOutlined />} onClick={toggleTheme} style={{ color: iconCol }} />
                </Tooltip>
                <Tooltip title={sshKeyAgeDays !== null && sshKeyAgeDays >= 75 ? `SSH key ${sshKeyAgeDays}d old — rotation recommended` : 'Settings'}>
                  <Badge dot={sshKeyAgeDays !== null && sshKeyAgeDays >= 75} color="orange" offset={[-4, 4]}>
                    <Button type="text" size="small" icon={<SettingOutlined />} onClick={() => setSettingsOpen(true)} style={{ color: iconCol }} />
                  </Badge>
                </Tooltip>
              </div>
            </div>

            {/* View area */}
            <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
              {terminals.map(t => (
                <div
                  key={t.id}
                  style={{ display: activeTerminalId === t.id ? 'flex' : 'none', flexDirection: 'column', flex: 1, minHeight: 0 }}
                >
                  <TerminalTab sessionId={t.id} host={t.host} />
                </div>
              ))}

              <div style={{ display: activeTerminalId === null ? 'flex' : 'none', flexDirection: 'column', flex: 1, minHeight: 0, padding: '24px 24px 16px', overflow: 'hidden' }}>
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
            </div>

            {activeTerminalId === null && selectedHostGroups.length > 0 && (
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
          onKeyChanged={loadSshKeyAge}
        />
      </ConfigProvider>
    </ThemeContext.Provider>
  )
}

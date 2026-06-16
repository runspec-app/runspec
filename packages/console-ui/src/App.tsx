import React, { useEffect, useState, Suspense } from 'react'
import { ConfigProvider, Badge, theme, Button, Tooltip, Tag, Dropdown, Spin } from 'antd'
import {
  ThunderboltOutlined,
  AppstoreOutlined,
  FormOutlined,
  HistoryOutlined,
  CalendarOutlined,
  BarChartOutlined,
  SunOutlined,
  MoonOutlined,
  SettingOutlined,
  SwapOutlined,
  NodeIndexOutlined,
  CodeOutlined,
  BellOutlined,
  KeyOutlined,
  LoadingOutlined,
  BugOutlined,
  FileTextOutlined,
  ReadOutlined,
} from '@ant-design/icons'
import { ConsoleView } from './views/ConsoleView'
import { SpecsView } from './views/SpecsView'
import { HistoryView } from './views/HistoryView'
import { LogsView } from './views/LogsView'
import { SchedulesView } from './views/SchedulesView'
import { TriggersView } from './views/TriggersView'
import { DevView } from './views/DevView'
import { FormsView, type PendingForm } from './views/FormsView'
import { TransferModal } from './views/TransferModal'
import { RelayModal } from './views/RelayModal'
import { CopyKeyModal } from './views/CopyKeyModal'
// Lazy-loaded: @ant-design/plots is heavy and only the Analytics tab needs it,
// so it stays out of the initial bundle.
const AnalyticsView = React.lazy(() => import('./views/AnalyticsView').then(m => ({ default: m.AnalyticsView })))
import { CommandInput } from './components/CommandInput'
import { SettingsDrawer } from './components/SettingsDrawer'
import { useInFlight } from './bridge/useInFlight'
import { bridge, type HistoryRecord, type Host, type Runnable } from './bridge'
import { ThemeContext } from './ThemeContext'

type ViewKey = 'console' | 'specs' | 'history' | 'logs' | 'forms' | 'schedules' | 'triggers' | 'analytics' | 'dev'

export default function App() {
  const [view, setView] = useState<ViewKey>('console')
  // Analytics is lazy-loaded; mount it on first visit, then keep it mounted so
  // its scanned data survives tab switches (without eager-loading the chunk).
  const [analyticsVisited, setAnalyticsVisited] = useState(false)
  const [isDark, setIsDark] = useState(() => localStorage.getItem('theme') !== 'light')
  const [autoSwitch, setAutoSwitch] = useState(() => localStorage.getItem('autoSwitch') !== 'false')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [transferOpen, setTransferOpen] = useState(false)
  const [relayOpen, setRelayOpen] = useState(false)
  const [copyKeyHost, setCopyKeyHost] = useState<string | null>(null)
  const [sshKeyAgeDays, setSshKeyAgeDays] = useState<number | null>(null)
  const [runnables, setRunnables] = useState<Runnable[]>([])
  const [runnablesLoading, setRunnablesLoading] = useState(true)
  const [hosts, setHosts] = useState<Host[]>([])
  const [selectedHost, setSelectedHost] = useState<string>('')
  const [inputHistory, setInputHistory] = useState<string[]>([])
  const [pendingChat, setPendingChat] = useState<string | null>(null)
  const [pendingForm, setPendingForm] = useState<PendingForm | null>(null)
  const [historySearch, setHistorySearch] = useState('')
  const [activeScope, setActiveScope] = useState<string[]>([])
  const [activeChatId, setActiveChatId] = useState<string | null>(null)
  const [version, setVersion] = useState<string>('')
  const [updateInfo, setUpdateInfo] = useState<{ latest: string | null; updateAvailable: boolean } | null>(null)
  const [docsUrl, setDocsUrl] = useState<string>('')
  // Triggers tab badge — pending human confirms + waiting (unclaimed) queue
  // conversations, so an approval or a new queue caller is visible from any tab.
  const [triggerAwaiting, setTriggerAwaiting] = useState(0)
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
  useEffect(() => { if (view === 'analytics') setAnalyticsVisited(true) }, [view])

  useEffect(() => {
    // Version renders instantly; the PyPI update check resolves in the
    // background and quietly stays hidden when the network is unreachable.
    bridge.app_version().then(setVersion).catch(() => {})
    bridge.docs_url().then(setDocsUrl).catch(() => {})
    // White-label title (branded to the active LLM plugin's package). The native
    // window title is set by the Python side; mirror it onto the document so the
    // page title matches too.
    bridge.app_title().then(t => { document.title = t }).catch(() => {})
    bridge.check_for_update()
      .then(r => { if (r.ok) setUpdateInfo({ latest: r.latest, updateAvailable: r.update_available }) })
      .catch(() => {})
  }, [])

  useEffect(() => {
    // Clear the loading flag whether the call resolves or rejects — otherwise a
    // failed/never-ready bridge call (e.g. right after a restart, before the
    // webview API is injected) would pin the per-host spinner on forever.
    bridge.get_runnables('all')
      .then(r => { setRunnables(r); setRunnablesLoading(false) })
      .catch(() => setRunnablesLoading(false))
    bridge.get_hosts().then(hs => {
      setHosts(hs)
      setSelectedHost(hs[0]?.name ?? '')
    })
    const onHostsUpdated = () => bridge.get_hosts().then(setHosts)
    // Discovery completion arrives Python→JS via the dispatch queue, so it lands
    // even if the initial JS→Python call stalled — also clear loading here.
    const onRunnablesUpdated = () =>
      bridge.get_runnables('all').then(r => { setRunnables(r); setRunnablesLoading(false) })
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

  const handleRunRunnable = (runnable: Runnable, args: Record<string, unknown>, commandPath: string[] = [], credentialId?: string) => {
    const cmd = commandPath.length > 0 ? `${runnable.name} ${commandPath.join(' ')}` : runnable.name
    const argStr = Object.entries(args).map(([k, v]) => v === true ? `--${k}` : `--${k}=${v}`).join(' ')
    const label = argStr ? `/${cmd} ${argStr}` : `/${cmd}`
    setInputHistory(h => [...h, label])
    setView('console')
    window.dispatchEvent(new CustomEvent('runspec:invoke_runnable', { detail: { runnable, args, commandPath, credentialId } }))
  }

  const handleOpenForm = (runnable: Runnable, commandPath: string[] = []) => {
    setPendingForm({ runnable, commandPath })
    setView('forms')
  }

  const handleSendChat = (message: string) => {
    setInputHistory(h => [...h, message])
    if (autoSwitch) { setView('console') }
    window.dispatchEvent(new CustomEvent('runspec:send_chat', { detail: { message } }))
  }

  // ConsoleView broadcasts the in-flight chat turn so the command bar's send
  // button can flip to a Stop control while the assistant is working.
  useEffect(() => {
    const onActive = (e: Event) => setActiveChatId((e as CustomEvent).detail?.id ?? null)
    window.addEventListener('runspec:chat_active', onActive)
    return () => window.removeEventListener('runspec:chat_active', onActive)
  }, [])

  // Triggers tab badge: a card counts when it's awaiting a human confirm
  // (awaiting_human column or a live tool_confirm) OR it's a waiting queue
  // conversation — a queue card (source 'chat') still 'open' (unclaimed). One
  // badge for both pending approvals and new callers. tool_confirm is filtered
  // to known card ids so plain chat confirms don't count.
  useEffect(() => {
    type Info = { column: string; source?: string; state?: string }
    const cardInfo = new Map<string, Info>()
    const pending = new Set<string>()           // card_ids awaiting confirm
    const recompute = () => {
      let n = 0
      for (const [id, info] of cardInfo) {
        const awaiting = pending.has(id) || info.column === 'awaiting_human'
        const queueWaiting = info.source === 'chat' && info.state === 'open'
        if (awaiting || queueWaiting) n++
      }
      setTriggerAwaiting(n)
    }
    const onCard = (e: Event) => {
      const c = (e as CustomEvent).detail as { card_id: string; column: string; source?: string; detail?: { state?: string } }
      cardInfo.set(c.card_id, { column: c.column, source: c.source, state: c.detail?.state })
      if (c.column !== 'awaiting_human') pending.delete(c.card_id)  // moved on
      recompute()
    }
    const onConfirm = (e: Event) => {
      const { id } = (e as CustomEvent).detail as { id: string }
      if (cardInfo.has(id)) { pending.add(id); recompute() }  // known cards only
    }
    const onEnd = (e: Event) => {
      const { id } = (e as CustomEvent).detail as { id: string }
      if (pending.delete(id)) recompute()
    }
    bridge.get_trigger_board().then(board => {
      board.forEach(c => cardInfo.set(c.card_id, { column: c.column, source: c.source, state: (c.detail as { state?: string } | undefined)?.state }))
      recompute()
    }).catch(() => {})
    window.addEventListener('runspec:trigger_card', onCard)
    window.addEventListener('runspec:tool_confirm', onConfirm)
    window.addEventListener('runspec:run_end', onEnd)
    return () => {
      window.removeEventListener('runspec:trigger_card', onCard)
      window.removeEventListener('runspec:tool_confirm', onConfirm)
      window.removeEventListener('runspec:run_end', onEnd)
    }
  }, [])

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
    { key: 'logs'      as ViewKey, label: 'Logs',      icon: <FileTextOutlined /> },
    { key: 'analytics' as ViewKey, label: 'Analytics', icon: <BarChartOutlined /> },
    { key: 'specs'     as ViewKey, label: 'Specs',     icon: <AppstoreOutlined /> },
    { key: 'forms'     as ViewKey, label: 'Forms',     icon: <FormOutlined /> },
    { key: 'schedules' as ViewKey, label: 'Schedules', icon: <CalendarOutlined /> },
    {
      key: 'triggers' as ViewKey,
      label: 'Triggers',
      icon: triggerAwaiting > 0
        ? <Badge count={triggerAwaiting} size="small" offset={[2, -1]}><BellOutlined /></Badge>
        : <BellOutlined />,
    },
    { key: 'dev'       as ViewKey, label: 'Dev',       icon: <BugOutlined /> },
  ]

  return (
    <ThemeContext.Provider value={isDark}>
      <ConfigProvider theme={{
          algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
          token: { fontFamily: 'monospace' },
        }}>
        <div style={{ height: '100vh', display: 'flex', flexDirection: 'column', overflow: 'hidden', background: contentBg, fontFamily: 'monospace' }}>
        <div style={{ flex: 1, display: 'flex', overflow: 'hidden', minHeight: 0 }}>
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
                      // Local: terminal only (when connected). Remote: "Copy SSH
                      // public key" is always offered — it's the bootstrap action
                      // for a host that's still disconnected because our key isn't
                      // in its authorized_keys yet — plus the SSH terminal once up.
                      items: h.name === 'local'
                        ? (h.connected ? [
                            {
                              key: 'open_terminal',
                              icon: <CodeOutlined />,
                              label: 'Open terminal',
                              onClick: () => bridge.launch_local_terminal().catch(console.error),
                            },
                          ] : [])
                        : [
                            {
                              key: 'copy_ssh_id',
                              icon: <KeyOutlined />,
                              label: 'Copy SSH public key…',
                              onClick: () => setCopyKeyHost(h.name),
                            },
                            ...(h.connected ? [{
                              key: 'open_terminal',
                              icon: <CodeOutlined />,
                              label: 'Open SSH terminal',
                              onClick: () => bridge.launch_terminal(h.name).catch(console.error),
                            }] : []),
                          ],
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
                        fontSize: 13, color: textCol, flex: 1,
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      }}>{h.name}</span>
                      {runnablesLoading && <LoadingOutlined style={{ fontSize: 10, color: '#595959' }} spin />}
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
            {/* Version + update indicator */}
            <div style={{
              flexShrink: 0,
              borderTop: `1px solid ${borderCol}`,
              padding: '7px 14px',
              display: 'flex', alignItems: 'center', gap: 6,
              fontSize: 11, color: iconCol,
            }}>
              <span style={{ fontFamily: 'monospace' }}>{version ? `v${version}` : ''}</span>
              {updateInfo?.updateAvailable ? (
                <Tooltip title={`Update available: v${updateInfo.latest} — open documentation`}>
                  <Tag
                    color="blue"
                    onClick={() => bridge.open_docs().catch(console.error)}
                    style={{ cursor: 'pointer', margin: 0, fontSize: 10, lineHeight: '16px', padding: '0 5px' }}
                  >
                    ↑ {updateInfo.latest}
                  </Tag>
                </Tooltip>
              ) : updateInfo ? (
                <Tooltip title="You're on the latest version">
                  <span style={{ color: '#52c41a' }}>up to date</span>
                </Tooltip>
              ) : null}
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

              <div style={{ display: 'flex', alignItems: 'center', gap: 2, paddingRight: 8 }}>
                <Tooltip title={docsUrl ? `Documentation — ${docsUrl}` : 'Documentation'}>
                  <Button type="text" size="small" icon={<ReadOutlined />} onClick={() => bridge.open_docs().catch(console.error)} style={{ color: iconCol }} />
                </Tooltip>
                <Tooltip title={isDark ? 'Light theme' : 'Dark theme'}>
                  <Button type="text" size="small" icon={isDark ? <SunOutlined /> : <MoonOutlined />} onClick={toggleTheme} style={{ color: iconCol }} />
                </Tooltip>
                <Tooltip title="Transfer files (SFTP)">
                  <Button type="text" size="small" icon={<SwapOutlined />} onClick={() => setTransferOpen(true)} style={{ color: iconCol }} />
                </Tooltip>
                <Tooltip title="Relay files between hosts">
                  <Button type="text" size="small" icon={<NodeIndexOutlined />} onClick={() => setRelayOpen(true)} style={{ color: iconCol }} />
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
              <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, padding: '24px 24px 16px', overflow: 'hidden' }}>
                <div style={{ display: view === 'console' ? 'flex' : 'none', flexDirection: 'column', flex: 1, minHeight: 0 }}>
                  <ConsoleView inFlight={inFlight} pendingChat={pendingChat} onChatSent={() => setPendingChat(null)} />
                </div>
                {/* Triggers stays mounted (display toggle) so a pending approval's
                    Approve/Deny buttons survive tab switches — unmounting it drops
                    the live tool_confirm and the buttons can't return until timeout. */}
                <div style={{ display: view === 'triggers' ? 'block' : 'none', flex: 1, overflow: 'auto' }}>
                  <TriggersView />
                </div>
                {/* History stays mounted (display toggle) so its fetched records
                    survive tab switches — returning shows the last data instantly
                    instead of unmounting and reloading from empty over SSH. Its
                    poll is gated on `active` so it only runs while visible. */}
                <div style={{ display: view === 'history' ? 'block' : 'none', flex: 1, overflow: 'auto' }}>
                  <HistoryView search={historySearch} onSearchChange={setHistorySearch} onRerun={handleHistoryRerun} onAskLlm={handleAskLlm} activeScope={activeScope} onScopeToggle={handleScopeToggle} selectedHost={selectedHost} active={view === 'history'} />
                </div>
                {/* Logs stays mounted (display toggle) so its logs_status SSH
                    inventory survives tab switches; its fetch is gated on `active`
                    so it only runs while visible and on host change. */}
                <div style={{ display: view === 'logs' ? 'block' : 'none', flex: 1, overflow: 'auto' }}>
                  <LogsView selectedHost={selectedHost} activeScope={activeScope} onScopeToggle={handleScopeToggle} active={view === 'logs'} />
                </div>
                {/* Analytics is lazy-loaded (heavy plots chunk). Mount it on first
                    visit, then keep it mounted (display toggle) so its scanned data
                    survives tab switches and it doesn't re-run the fleet scan on
                    every return; its poll is gated on `active` so it only runs
                    while visible. */}
                {analyticsVisited && (
                  <div style={{ display: view === 'analytics' ? 'block' : 'none', flex: 1, overflow: 'auto' }}>
                    <Suspense fallback={<div style={{ display: 'flex', justifyContent: 'center', padding: 80 }}><Spin size="large" /></div>}>
                      <AnalyticsView selectedHost={selectedHost} activeScope={activeScope} active={view === 'analytics'} />
                    </Suspense>
                  </div>
                )}
                {view !== 'console' && view !== 'triggers' && view !== 'history' && view !== 'analytics' && view !== 'logs' && (
                  <div style={{ flex: 1, overflow: 'auto' }}>
                    {view === 'specs'     && <SpecsView runnables={runnables} selectedHost={selectedHost} activeScope={activeScope} onScopeToggle={handleScopeToggle} />}
                    {view === 'forms'     && <FormsView runnables={runnables} hosts={hosts} selectedHost={selectedHost} activeScope={activeScope} onRunRunnable={handleRunRunnable} pendingForm={pendingForm} onPendingFormClear={() => setPendingForm(null)} />}
                    {view === 'schedules' && <SchedulesView hosts={hosts} runnables={runnables} selectedHost={selectedHost} />}
                    {view === 'dev'       && <DevView />}
                  </div>
                )}
              </div>
            </div>

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
                activeChatId={activeChatId}
                onStopChat={(id) => { bridge.cancel_chat(id).catch(console.error) }}
                history={inputHistory}
                autoSwitch={autoSwitch}
                onToggleAutoSwitch={toggleAutoSwitch}
              />
            </div>

          </div>
        </div>
        </div>

        <TransferModal
          open={transferOpen}
          onClose={() => setTransferOpen(false)}
          hosts={hosts}
          defaultRemoteHost={selectedHost}
        />

        <RelayModal
          open={relayOpen}
          onClose={() => setRelayOpen(false)}
          hosts={hosts}
        />

        <CopyKeyModal
          open={copyKeyHost !== null}
          host={copyKeyHost ?? ''}
          onClose={() => setCopyKeyHost(null)}
          onDone={() => {
            bridge.get_hosts().then(setHosts)
            bridge.get_runnables('all').then(setRunnables)
          }}
        />

        <SettingsDrawer
          open={settingsOpen}
          onClose={() => setSettingsOpen(false)}
          connectedHosts={hosts.filter(h => h.connected && h.name !== 'local' && h.role !== undefined).map(h => h.name)}
          allRemoteHosts={hosts.filter(h => h.name !== 'local' && h.role !== undefined).map(h => h.name)}
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

import React, { useEffect, useState, useMemo, Suspense } from 'react'
import { ConfigProvider, Badge, theme, Button, Tooltip, Tag, Dropdown, Spin, Popover, Select, message, Watermark } from 'antd'
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
  ClockCircleOutlined,
  KeyOutlined,
  LoadingOutlined,
  BugOutlined,
  FileTextOutlined,
  ReadOutlined,
  DownOutlined,
  RightOutlined,
  CheckCircleOutlined,
  CommentOutlined,
} from '@ant-design/icons'
import { ConsoleView } from './views/ConsoleView'
import { SpecsView } from './views/SpecsView'
import { HistoryView } from './views/HistoryView'
import { LogsView } from './views/LogsView'
import { SchedulesView } from './views/SchedulesView'
import { TriggersView } from './views/TriggersView'
import { RotaView } from './views/RotaView'
import { DevView } from './views/DevView'
import { FormsView, type PendingForm } from './views/FormsView'
import { TransferModal } from './views/TransferModal'
import { RelayModal } from './views/RelayModal'
import { CopyKeyModal } from './views/CopyKeyModal'
import { RunbookRunModal } from './views/RunbookRunModal'
// Lazy-loaded: @ant-design/plots is heavy and only the Analytics tab needs it,
// so it stays out of the initial bundle.
const AnalyticsView = React.lazy(() => import('./views/AnalyticsView').then(m => ({ default: m.AnalyticsView })))
import { CommandInput } from './components/CommandInput'
import { SettingsDrawer } from './components/SettingsDrawer'
import { RUNNING_STATUSES } from './components/runShared'
import { useInFlight } from './bridge/useInFlight'
import { bridge, type HistoryRecord, type Host, type HostProfile, type Runnable, type Runbook, type RoomInfo, type BrandTheme } from './bridge'
import { ThemeContext } from './ThemeContext'

type ViewKey = 'console' | 'specs' | 'history' | 'logs' | 'forms' | 'schedules' | 'triggers' | 'rota' | 'analytics' | 'dev'

export default function App() {
  const [view, setView] = useState<ViewKey>('console')
  // Analytics is lazy-loaded; mount it on first visit, then keep it mounted so
  // its scanned data survives tab switches (without eager-loading the chunk).
  const [analyticsVisited, setAnalyticsVisited] = useState(false)
  const [isDark, setIsDark] = useState(() => localStorage.getItem('theme') !== 'light')
  const [autoSwitch, setAutoSwitch] = useState(() => localStorage.getItem('autoSwitch') !== 'false')
  // Which sidebar host groups are collapsed (group name → true). Persisted so the
  // layout survives reloads; a group absent from the map is expanded.
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>(() => {
    try {
      return JSON.parse(localStorage.getItem('runspec.sidebar.collapsedGroups') || '{}')
    } catch {
      return {}
    }
  })
  const toggleGroup = (g: string) => setCollapsedGroups(prev => {
    const next = { ...prev, [g]: !prev[g] }
    localStorage.setItem('runspec.sidebar.collapsedGroups', JSON.stringify(next))
    return next
  })
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [transferOpen, setTransferOpen] = useState(false)
  const [relayOpen, setRelayOpen] = useState(false)
  const [copyKeyHost, setCopyKeyHost] = useState<string | null>(null)
  const [sshKeyAgeDays, setSshKeyAgeDays] = useState<number | null>(null)
  const [runnables, setRunnables] = useState<Runnable[]>([])
  const [runnablesLoading, setRunnablesLoading] = useState(true)
  // Fleet-discovery readiness — drives the "discovering N/M hosts…" indicator
  // above the composer so a first turn that races discovery is visibly explained
  // (the agent is blind until the catalog is populated). Cleared once ready.
  const [discovery, setDiscovery] = useState<{ ready: boolean; done: number; total: number }>(
    { ready: true, done: 0, total: 0 },
  )
  const [hosts, setHosts] = useState<Host[]>([])
  const [selectedHost, setSelectedHost] = useState<string>('')
  // Host profiles (working-sets): the registry to pick from + the machine-local
  // active one. Switching tears down the old scope's connections and brings up
  // the new — see docs/design/host-profiles.md.
  const [profiles, setProfiles] = useState<HostProfile[]>([])
  const [activeProfile, setActiveProfile] = useState<string>('')
  // Configured chat/helpdesk rooms + their live connection state, for the footer
  // indicator. Updated on every runspec:room_connection flip so a room server
  // going down (or coming back) is visible without digging through Dev logs.
  const [rooms, setRooms] = useState<RoomInfo[]>([])
  const [inputHistory, setInputHistory] = useState<string[]>([])
  const [pendingChat, setPendingChat] = useState<string | null>(null)
  const [pendingForm, setPendingForm] = useState<PendingForm | null>(null)
  const [runbooks, setRunbooks] = useState<Runbook[]>([])
  const [pendingRunbook, setPendingRunbook] = useState<Runbook | null>(null)
  const [historySearch, setHistorySearch] = useState('')
  const [activeScope, setActiveScope] = useState<string[]>([])
  const [activeChatId, setActiveChatId] = useState<string | null>(null)
  const [version, setVersion] = useState<string>('')
  const [updateInfo, setUpdateInfo] = useState<{ latest: string | null; updateAvailable: boolean } | null>(null)
  const [docsUrl, setDocsUrl] = useState<string>('')
  // White-label theme ([console.theme]) — accent colour, watermark, sidebar brand
  // name. Read-only (set by a config seed or by hand); {} = the default look.
  const [brandTheme, setBrandTheme] = useState<BrandTheme>({})
  // Triggers tab badge — pending human confirms + waiting (unclaimed) queue
  // conversations, so an approval or a new queue caller is visible from any tab.
  const [triggerAwaiting, setTriggerAwaiting] = useState(0)
  // Schedules badge: how many schedule runs are currently in flight (mirrors the
  // Triggers badge). Lives here, not in SchedulesView, since that view unmounts.
  const [scheduleRunning, setScheduleRunning] = useState(0)
  const inFlight = useInFlight()

  const selectedHostObj = hosts.find(h => h.name === selectedHost)
  const selectedHostGroups = selectedHostObj?.groups ?? []
  const hostFilteredRunnables = runnables.filter(r => r.host === selectedHost)
  const scopedRunnables = activeScope.length > 0
    ? hostFilteredRunnables.filter(r => activeScope.includes(r.group))
    : hostFilteredRunnables

  // Catalogue stats for the footer indicator: total tools, distinct hosts/venvs,
  // and a per-host → per-venv breakdown for the hover popover. Recomputed when the
  // runnables cache changes — so the count visibly grows as discovery streams hosts
  // in on startup, and settles at a number you learn to expect.
  const catalogue = useMemo(() => {
    const byHost = new Map<string, Map<string, number>>()
    for (const r of runnables) {
      let venvs = byHost.get(r.host)
      if (!venvs) { venvs = new Map(); byHost.set(r.host, venvs) }
      venvs.set(r.group, (venvs.get(r.group) ?? 0) + 1)
    }
    const hostsBreakdown = [...byHost.entries()]
      .map(([host, venvs]) => ({
        host,
        total: [...venvs.values()].reduce((a, b) => a + b, 0),
        venvs: [...venvs.entries()]
          .map(([group, count]) => ({ group, count }))
          .sort((a, b) => b.count - a.count || a.group.localeCompare(b.group)),
      }))
      .sort((a, b) => b.total - a.total || a.host.localeCompare(b.host))
    const venvCount = hostsBreakdown.reduce((n, h) => n + h.venvs.length, 0)
    const byHostTotal: Record<string, number> = {}
    const byHostVenvs: Record<string, { group: string; count: number }[]> = {}
    for (const h of hostsBreakdown) { byHostTotal[h.host] = h.total; byHostVenvs[h.host] = h.venvs }
    return { total: runnables.length, hostCount: byHost.size, venvCount, hostsBreakdown, byHostTotal, byHostVenvs }
  }, [runnables])

  // Briefly highlight the footer catalogue count whenever it changes, so the eye
  // catches it climbing during startup discovery (and any later refresh delta).
  const [countFlash, setCountFlash] = useState(false)
  const prevTotalRef = React.useRef<number | null>(null)
  useEffect(() => {
    const prev = prevTotalRef.current
    prevTotalRef.current = catalogue.total
    if (prev === null || prev === catalogue.total) return  // skip first paint / no-op
    setCountFlash(true)
    const t = setTimeout(() => setCountFlash(false), 700)
    return () => clearTimeout(t)
  }, [catalogue.total])

  const handleScopeToggle = (group: string) => {
    setActiveScope(prev =>
      prev.includes(group) ? prev.filter(g => g !== group) : [...prev, group]
    )
  }

  const handleHostSelect = (name: string) => {
    setSelectedHost(name)
    setActiveScope([])
  }

  // Switch the active host profile (working-set). "" = All hosts. The backend
  // tears down the leaving hosts' connections and re-probes the new scope, then
  // dispatches runspec:hosts_updated which refreshes the sidebar + active value.
  const handleSwitchProfile = (name: string) => {
    setActiveProfile(name)  // optimistic — corrected by the hosts_updated refetch
    bridge.set_active_profile(name).catch(() => bridge.get_active_profile().then(setActiveProfile))
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
    bridge.brand_theme().then(setBrandTheme).catch(() => {})
    // Always resolve the indicator — on `ok: false` (offline / private index
    // unreachable, after the backend's pip timeout) keep `latest` null so the
    // footer stops "checking…" without falsely claiming "up to date".
    bridge.check_for_update()
      .then(r => setUpdateInfo({ latest: r.ok ? r.latest : null, updateAvailable: r.ok ? r.update_available : false }))
      .catch(() => setUpdateInfo({ latest: null, updateAvailable: false }))
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
    // Host-profile switcher: load the registry + the active selection, and keep
    // them fresh as the registry changes (Settings edits / Config Sync pulls).
    const loadProfiles = () => {
      bridge.get_profiles?.().then(setProfiles).catch(() => {})
      bridge.get_active_profile?.().then(setActiveProfile).catch(() => {})
    }
    loadProfiles()
    // Discovery completion arrives Python→JS via the dispatch queue, so it lands
    // even if the initial JS→Python call stalled — also clear loading here.
    const onRunnablesUpdated = () =>
      bridge.get_runnables('all').then(r => { setRunnables(r); setRunnablesLoading(false) })
    // Runbooks feed the `$` menu + run modal; refresh after the editor saves or a
    // Config Sync pull lands new ones (gitsync dispatches runspec:runbooks_changed).
    const loadRunbooks = () => bridge.get_runbooks().then(setRunbooks).catch(() => {})
    loadRunbooks()
    // Discovery readiness: read on mount (events may predate this listener), then
    // track live as each host completes.
    bridge.discovery_state?.().then(setDiscovery).catch(() => {})
    const onDiscoveryState = (e: Event) => {
      const d = (e as CustomEvent).detail as { ready: boolean; done: number; total: number }
      if (d) setDiscovery(d)
    }
    // Room connection state: read on mount, then refetch on every connect/drop
    // flip (a refetch is cheap and always reflects the full configured set, incl.
    // a newly added room) so the footer indicator stays live.
    const loadRooms = () => bridge.get_room_state().then(s => setRooms(s.rooms)).catch(() => {})
    loadRooms()
    window.addEventListener('runspec:hosts_updated', onHostsUpdated)
    window.addEventListener('runspec:hosts_updated', loadProfiles)
    window.addEventListener('runspec:runnables_updated', onRunnablesUpdated)
    window.addEventListener('runspec:runbooks_changed', loadRunbooks)
    window.addEventListener('runspec:discovery_state', onDiscoveryState)
    window.addEventListener('runspec:room_connection', loadRooms)
    return () => {
      window.removeEventListener('runspec:hosts_updated', onHostsUpdated)
      window.removeEventListener('runspec:hosts_updated', loadProfiles)
      window.removeEventListener('runspec:runnables_updated', onRunnablesUpdated)
      window.removeEventListener('runspec:runbooks_changed', loadRunbooks)
      window.removeEventListener('runspec:discovery_state', onDiscoveryState)
      window.removeEventListener('runspec:room_connection', loadRooms)
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
      detail: { host: record.host, runnable: record.runnable, args: record.args, commandPath: record.commandPath },
    }))
  }

  const handleAskLlm = (text: string) => {
    setView('console')
    setPendingChat(text)
  }

  const handleRunRunnable = (runnable: Runnable, args: Record<string, unknown>, commandPath: string[] = [], credentialIds?: string[]) => {
    const cmd = commandPath.length > 0 ? `${runnable.name} ${commandPath.join(' ')}` : runnable.name
    const argStr = Object.entries(args).map(([k, v]) => v === true ? `--${k}` : `--${k}=${v}`).join(' ')
    const label = argStr ? `/${cmd} ${argStr}` : `/${cmd}`
    setInputHistory(h => [...h, label])
    setView('console')
    window.dispatchEvent(new CustomEvent('runspec:invoke_runnable', { detail: { runnable, args, commandPath, credentialIds } }))
  }

  const handleOpenForm = (runnable: Runnable, commandPath: string[] = []) => {
    setPendingForm({ runnable, commandPath })
    setView('forms')
  }

  // Edit-and-rerun (Console card pencil / History "Edit & rerun") routes here: look
  // up the full discovered runnable so the Forms run dialog can render its rich
  // typed inputs — selectors, switches, path pickers — pre-filled with the
  // previous run's values, instead of a flat plain-text arg editor.
  useEffect(() => {
    const onEditInForm = (e: Event) => {
      const { host, runnable, args, commandPath = [] } =
        (e as CustomEvent).detail as { host: string; runnable: string; args: Record<string, unknown>; commandPath?: string[] }
      const found = runnables.find(r => r.host === host && r.name === runnable)
      if (!found) {
        message.warning(`Can't open a form for "${runnable}" — it isn't currently discovered on ${host}.`)
        return
      }
      setPendingForm({ runnable: found, commandPath, prefill: args })
      setView('forms')
    }
    window.addEventListener('runspec:edit_in_form', onEditInForm)
    return () => window.removeEventListener('runspec:edit_in_form', onEditInForm)
  }, [runnables])

  const handleSendChat = (message: string) => {
    setInputHistory(h => [...h, message])
    if (autoSwitch) { setView('console') }
    window.dispatchEvent(new CustomEvent('runspec:send_chat', { detail: { message } }))
  }

  const handleOpenRunbook = (runbook: Runbook) => setPendingRunbook(runbook)

  // The run modal collected values; hand off to ConsoleView (it owns the
  // bridge.run_runbook call + the transcript block, mirroring runspec:send_chat).
  const handleRunRunbook = (runbookId: string, values: Record<string, unknown>, extra: string) => {
    setInputHistory(h => [...h, `$${runbookId}`])
    if (autoSwitch) { setView('console') }
    window.dispatchEvent(new CustomEvent('runspec:run_runbook', { detail: { runbookId, values, extra } }))
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

  // Schedules tab badge: count schedule run cards still in flight (mirrors the
  // Triggers badge), driven by runspec:schedule_card with run_end clearing them.
  useEffect(() => {
    const cols = new Map<string, string>()  // card_id → lifecycle status
    const recompute = () => setScheduleRunning(
      [...cols.values()].filter(c => RUNNING_STATUSES.has(c)).length,
    )
    const onCard = (e: Event) => {
      const c = (e as CustomEvent).detail as { card_id: string; column: string }
      cols.set(c.card_id, c.column)
      recompute()
    }
    bridge.get_schedule_board().then(board => {
      board.forEach(c => cols.set(c.card_id, c.column))
      recompute()
    }).catch(() => {})
    window.addEventListener('runspec:schedule_card', onCard)
    return () => window.removeEventListener('runspec:schedule_card', onCard)
  }, [])

  const headerBg  = isDark ? '#0d0d0d' : '#fafafa'
  const siderBg   = isDark ? '#111'    : '#fafafa'
  const borderCol = isDark ? '#222'    : '#e0e0e0'
  // The app's signature accent — sidebar title, active nav tab, section/room
  // highlights. A white-label accent_color (when set) drives it too, so the
  // brand colour shows where it's actually visible (not just Ant's colorPrimary
  // controls). Falls back to the built-in blue when no theme is set.
  const titleCol  = brandTheme.accent_color || (isDark ? '#4fc1ff' : '#0958d9')
  const contentBg = isDark ? '#111'    : '#ffffff'
  const iconCol   = isDark ? '#888'    : '#595959'
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
    {
      key: 'schedules' as ViewKey,
      label: 'Schedules',
      icon: scheduleRunning > 0
        ? <Badge count={scheduleRunning} size="small" offset={[2, -1]}><CalendarOutlined /></Badge>
        : <CalendarOutlined />,
    },
    {
      key: 'triggers' as ViewKey,
      label: 'Triggers',
      icon: triggerAwaiting > 0
        ? <Badge count={triggerAwaiting} size="small" offset={[2, -1]}><BellOutlined /></Badge>
        : <BellOutlined />,
    },
    { key: 'rota'      as ViewKey, label: 'Rota',      icon: <ClockCircleOutlined /> },
    { key: 'dev'       as ViewKey, label: 'Dev',       icon: <BugOutlined /> },
  ]

  return (
    <ThemeContext.Provider value={isDark}>
      <ConfigProvider theme={{
          algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
          token: {
            fontFamily: 'monospace',
            // White-label accent → drives buttons, links, active tabs, switches.
            ...(brandTheme.accent_color ? { colorPrimary: brandTheme.accent_color } : {}),
          },
        }}>
        {/* White-label content watermark (faint tiled brand mark). When neither
            text nor logo is set, Watermark renders nothing — a passthrough. The
            font colour is theme-aware: Ant's default rgba(0,0,0,.15) is invisible
            on the dark UI, so brighten it on dark. */}
        <Watermark
          content={brandTheme.watermark_text}
          image={brandTheme.watermark_logo}
          font={{ color: isDark ? 'rgba(255,255,255,0.12)' : 'rgba(0,0,0,0.10)' }}
        >
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
              {brandTheme.brand_name || 'runspec'}
            </div>
            {/* Host-profile switcher — only when profiles are defined. Picking one
                scopes the sidebar + standing connections to that working-set. */}
            {profiles.length > 0 && (
              <div style={{ padding: '8px 10px', borderBottom: `1px solid ${borderCol}`, flexShrink: 0 }}>
                <Select
                  size="small"
                  value={activeProfile}
                  onChange={handleSwitchProfile}
                  style={{ width: '100%' }}
                  popupMatchSelectWidth={false}
                  options={[
                    { value: '', label: 'All hosts' },
                    ...profiles.map(p => ({ value: p.name, label: p.name })),
                  ]}
                  title="Active host profile"
                />
              </div>
            )}
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
                      {(() => {
                        const count = catalogue.byHostTotal[h.name]
                        if (!count) return null
                        const venvs = catalogue.byHostVenvs[h.name] ?? []
                        return (
                          <Tooltip
                            title={
                              <div style={{ fontSize: 11 }}>
                                {count} runnable{count === 1 ? '' : 's'}
                                {venvs.map(v => (
                                  <div key={v.group} style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
                                    <span style={{ fontFamily: 'monospace' }}>{v.group}</span>
                                    <span>{v.count}</span>
                                  </div>
                                ))}
                              </div>
                            }
                          >
                            <span style={{ fontSize: 10, color: iconCol, fontVariantNumeric: 'tabular-nums', flexShrink: 0 }}>
                              {count}
                            </span>
                          </Tooltip>
                        )
                      })()}
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
                    {sortedGroupKeys.map(g => {
                      const members = groupMap.get(g)!
                      const collapsed = !!collapsedGroups[g]
                      const up = members.filter(m => m.connected).length
                      const down = members.length - up
                      return (
                        <div key={g || '__hosts__'}>
                          <div
                            onClick={() => toggleGroup(g)}
                            style={{
                              display: 'flex', alignItems: 'center', gap: 6,
                              padding: '10px 14px 3px',
                              fontSize: 10, color: isDark ? '#aaa' : '#737373',
                              textTransform: 'uppercase', letterSpacing: '0.08em',
                              userSelect: 'none', cursor: 'pointer',
                            }}
                          >
                            {collapsed
                              ? <RightOutlined style={{ fontSize: 8 }} />
                              : <DownOutlined style={{ fontSize: 8 }} />}
                            <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {g || 'Hosts'}
                            </span>
                            {collapsed && (
                              <span style={{ display: 'flex', alignItems: 'center', gap: 6, letterSpacing: 0 }}>
                                <span style={{ color: '#52c41a' }}>● {up}</span>
                                {down > 0 && <span style={{ color: '#595959' }}>● {down}</span>}
                              </span>
                            )}
                          </div>
                          {!collapsed && members.map(hostRow)}
                        </div>
                      )
                    })}
                  </>
                )
              })()}
            </div>
            {/* Catalogue size + version/update footer */}
            <div style={{
              flexShrink: 0,
              borderTop: `1px solid ${borderCol}`,
              padding: '6px 14px',
              display: 'flex', flexDirection: 'column', gap: 3,
              fontSize: 11, color: iconCol,
            }}>
              {/* Rooms: live connection state for every configured chat/helpdesk
                  room, so a room server going down is visible at a glance instead
                  of buried in the Dev tab. Hidden entirely when none are set. */}
              {rooms.length > 0 && (() => {
                const up = rooms.filter(r => r.connected).length
                const allUp = up === rooms.length
                const dot = allUp ? '#52c41a' : (up === 0 ? '#ff4d4f' : '#faad14')
                return (
                  <Popover
                    placement="topLeft"
                    mouseEnterDelay={0.2}
                    content={
                      <div style={{ maxWidth: 300, maxHeight: 320, overflowY: 'auto', fontSize: 12 }}>
                        <div style={{ marginBottom: 6, color: iconCol }}>
                          {up}/{rooms.length} room{rooms.length === 1 ? '' : 's'} connected
                        </div>
                        {rooms.map(r => (
                          <div key={`${r.host}/${r.room}`} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 4 }}>
                            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              <span style={{ color: r.connected ? '#52c41a' : '#ff4d4f', marginRight: 6 }}>●</span>
                              <span style={{ color: titleCol }}>{r.queue || r.room}</span>
                              <span style={{ color: iconCol }}> · {r.host}</span>
                            </span>
                            <span style={{ color: r.connected ? '#52c41a' : '#ff4d4f', flexShrink: 0 }}>
                              {r.connected ? 'connected' : 'down'}
                            </span>
                          </div>
                        ))}
                        {up < rooms.length && (
                          <div style={{ marginTop: 6, color: iconCol, fontSize: 11 }}>
                            A “down” room means its server isn’t reachable on the host — check the room process is running on its port.
                          </div>
                        )}
                      </div>
                    }
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 5, cursor: 'default' }}>
                      <CommentOutlined style={{ fontSize: 11, color: dot }} />
                      <span>
                        <span style={{ fontVariantNumeric: 'tabular-nums', color: titleCol }}>{up}/{rooms.length}</span>
                        {' '}room{rooms.length === 1 ? '' : 's'}
                        {!allUp && <span style={{ color: dot }}> · {rooms.length - up} down</span>}
                      </span>
                    </div>
                  </Popover>
                )
              })()}
              {/* Catalogue: total tools, growing live while discovery streams hosts in */}
              <Popover
                placement="topLeft"
                mouseEnterDelay={0.2}
                content={
                  <div style={{ maxWidth: 280, maxHeight: 320, overflowY: 'auto', fontSize: 12 }}>
                    <div style={{ marginBottom: 6, color: iconCol }}>
                      {catalogue.total} runnable{catalogue.total === 1 ? '' : 's'} across{' '}
                      {catalogue.venvCount} venv{catalogue.venvCount === 1 ? '' : 's'} on{' '}
                      {catalogue.hostCount} host{catalogue.hostCount === 1 ? '' : 's'}
                      {!discovery.ready && discovery.total > 0 && (
                        <span> · still loading ({discovery.done}/{discovery.total} hosts)</span>
                      )}
                    </div>
                    {catalogue.hostsBreakdown.length === 0 ? (
                      <div style={{ color: iconCol }}>No runnables discovered yet.</div>
                    ) : catalogue.hostsBreakdown.map(h => (
                      <div key={h.host} style={{ marginBottom: 5 }}>
                        <div style={{ fontWeight: 600, color: titleCol }}>
                          {h.host} <span style={{ fontWeight: 400, color: iconCol }}>· {h.total}</span>
                        </div>
                        <div style={{ paddingLeft: 8 }}>
                          {h.venvs.map(v => (
                            <div key={v.group} style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                              <span style={{ fontFamily: 'monospace', color: iconCol, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v.group}</span>
                              <span style={{ color: iconCol }}>{v.count}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                }
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 5, cursor: 'default' }}>
                  {!discovery.ready && discovery.total > 0
                    ? <LoadingOutlined spin style={{ fontSize: 11 }} />
                    : <AppstoreOutlined style={{ fontSize: 11 }} />}
                  <span>
                    <span style={{
                      fontVariantNumeric: 'tabular-nums',
                      color: countFlash ? '#52c41a' : titleCol,
                      fontWeight: countFlash ? 600 : 400,
                      transition: 'color 0.6s ease, font-weight 0.6s ease',
                    }}>{catalogue.total}</span>
                    {' '}tool{catalogue.total === 1 ? '' : 's'}
                    {catalogue.venvCount > 0 && <span> · {catalogue.venvCount} venv{catalogue.venvCount === 1 ? '' : 's'}</span>}
                    {!discovery.ready && discovery.total > 0 && (
                      <span style={{ opacity: 0.7 }}> · {discovery.done}/{discovery.total} hosts</span>
                    )}
                  </span>
                </div>
              </Popover>

              {/* Version + update state */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <Tooltip title={version ? `runspec-console v${version} — open documentation & release notes` : ''}>
                  <span
                    onClick={() => bridge.open_docs().catch(console.error)}
                    style={{ fontFamily: 'monospace', cursor: 'pointer' }}
                  >
                    {version ? `v${version}` : ''}
                  </span>
                </Tooltip>
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
                ) : updateInfo?.latest ? (
                  <Tooltip title="You're on the latest version">
                    <span style={{ color: '#52c41a', display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                      <CheckCircleOutlined style={{ fontSize: 11 }} /> up to date
                    </span>
                  </Tooltip>
                ) : updateInfo ? null : (
                  <Tooltip title="Checking for updates…">
                    <span style={{ opacity: 0.6, display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                      <LoadingOutlined spin style={{ fontSize: 10 }} /> checking…
                    </span>
                  </Tooltip>
                )}
              </div>
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
                    {view === 'forms'     && <FormsView runnables={runnables} hosts={hosts} selectedHost={selectedHost} activeScope={activeScope} onRunRunnable={handleRunRunnable} pendingForm={pendingForm} onPendingFormClear={() => setPendingForm(null)} runbooks={runbooks} onOpenRunbook={handleOpenRunbook} />}
                    {view === 'schedules' && <SchedulesView hosts={hosts} runnables={runnables} selectedHost={selectedHost} />}
                    {view === 'rota'      && <RotaView />}
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
              {!discovery.ready && discovery.total > 0 && (
                <div style={{
                  display: 'flex', alignItems: 'center', gap: 6,
                  margin: '0 0 6px', fontSize: 11, color: isDark ? '#8c8c8c' : '#8c8c8c',
                }}>
                  <LoadingOutlined spin style={{ fontSize: 11 }} />
                  <span>Discovering fleet… {discovery.done}/{discovery.total} hosts. The assistant sees more runnables as they load.</span>
                </div>
              )}
              <CommandInput
                runnables={scopedRunnables}
                runbooks={runbooks}
                onRunRunnable={handleRunRunnable}
                onOpenForm={handleOpenForm}
                onOpenRunbook={handleOpenRunbook}
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
        </Watermark>

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

        <RunbookRunModal
          runbook={pendingRunbook}
          hostNames={hosts.map(h => h.name)}
          onClose={() => setPendingRunbook(null)}
          onRun={handleRunRunbook}
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

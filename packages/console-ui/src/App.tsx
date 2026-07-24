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
  DashboardOutlined,
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
import { CopyKeyModal } from './views/CopyKeyModal'
import { RunbookRunModal } from './views/RunbookRunModal'
import { SavedRunRunModal } from './views/SavedRunRunModal'
// Lazy-loaded: @ant-design/plots is heavy and only the Analytics tab needs it,
// so it stays out of the initial bundle.
const AnalyticsView = React.lazy(() => import('./views/AnalyticsView').then(m => ({ default: m.AnalyticsView })))
import { CommandInput } from './components/CommandInput'
import { SettingsDrawer } from './components/SettingsDrawer'
import { RUNNING_STATUSES } from './components/runShared'
import { useInFlight } from './bridge/useInFlight'
import { bridge, type HistoryRecord, type Host, type HostProfile, type Runnable, type Runbook, type SavedRun, type RoomInfo, type BrandTheme } from './bridge'
import { ThemeContext, AccentContext, DEFAULT_ACCENT, UI_FONT, textMuted, textSecondary, textFaint } from './ThemeContext'

type ViewKey = 'console' | 'specs' | 'history' | 'logs' | 'forms' | 'schedules' | 'triggers' | 'rota' | 'analytics' | 'dev'

// Context-window fill of the active chat's latest turn (runspec:context_estimate).
// total_tokens is exact (from the model's usage); bucket tokens are estimates that
// sum to it. See docs/design/console-context-indicator.md.
interface ContextEstimate {
  model: string
  window: number
  total_tokens: number
  cache_hit_pct: number
  buckets: Array<{ key: string; tokens: number; est: boolean }>
  // Phase 2 levers: drive the search-mode nudge, the live-compaction button, and
  // the pinned-memory hint. Optional so older backends degrade gracefully.
  tool_mode?: string
  history_msgs?: number
  memory_pins?: number
  memory_pins_shown?: number
}

// Human label for each context bucket key the backend emits.
const CTX_BUCKET_LABELS: Record<string, string> = {
  system: 'System base',
  console_context: 'Console context',
  memory: 'Memory (pinned)',
  tools: 'Tool definitions',
  catalog: 'Catalog index',
  history: 'History',
}

/** Compact token count for the gauge (e.g. "48.2k", "980"). */
function fmtTokens(n: number): string {
  if (n < 1000) return `${n}`
  return `${(n / 1000).toFixed(1)}k`
}

const SIDEBAR_WIDTH = 180

// White-label content watermark. `tiled` (default) is Ant's repeated/slanted
// mark; `banner` / `center` draw a single large faint mark over the content area
// (banner = upper third, center = middle) — your "brand banner" look. All honour
// a logo image instead of text, are non-interactive (pointer-events: none), and
// brighten on the dark UI. A passthrough when no text/logo is set.
function BrandWatermark(
  { theme: t, isDark, children }: { theme: BrandTheme; isDark: boolean; children: React.ReactNode },
) {
  const text = t.watermark_text
  const logo = t.watermark_logo
  if (!text && !logo) return <>{children}</>

  const style = t.watermark_style ?? 'tiled'
  if (style === 'tiled') {
    // Theme-aware colour: Ant's default rgba(0,0,0,.15) is invisible on dark.
    return (
      <Watermark content={text} image={logo} font={{ color: isDark ? 'rgba(255,255,255,0.12)' : 'rgba(0,0,0,0.10)' }}>
        {children}
      </Watermark>
    )
  }

  // Single large mark over the content area (right of the sidebar).
  const centered = style === 'center'
  const markColor = isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)'
  return (
    <div style={{ position: 'relative', height: '100vh', overflow: 'hidden' }}>
      {children}
      <div aria-hidden style={{
        position: 'absolute', top: 0, bottom: 0, right: 0, left: SIDEBAR_WIDTH,
        display: 'flex', justifyContent: 'center',
        alignItems: centered ? 'center' : 'flex-start',
        paddingTop: centered ? 0 : '13vh',
        pointerEvents: 'none', userSelect: 'none', overflow: 'hidden', zIndex: 1,
      }}>
        {logo
          ? <img src={logo} alt="" style={{ maxWidth: '45%', maxHeight: '35%', opacity: isDark ? 0.1 : 0.08 }} />
          : <span style={{
              fontSize: 'clamp(28px, 6.5vw, 92px)', fontWeight: 800, letterSpacing: 4,
              textTransform: 'uppercase', whiteSpace: 'nowrap', color: markColor,
            }}>{text}</span>}
      </div>
    </div>
  )
}

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
  // Context-window fill of the active chat (latest turn), for the footer gauge +
  // composition hover. Null until a chat reports usage, so the row stays hidden
  // on non-chat tabs (mirrors the Rooms row hiding when empty). See
  // docs/design/console-context-indicator.md.
  const [ctx, setCtx] = useState<ContextEstimate | null>(null)
  const [inputHistory, setInputHistory] = useState<string[]>([])
  const [pendingChat, setPendingChat] = useState<string | null>(null)
  const [pendingForm, setPendingForm] = useState<PendingForm | null>(null)
  const [runbooks, setRunbooks] = useState<Runbook[]>([])
  const [pendingRunbook, setPendingRunbook] = useState<Runbook | null>(null)
  const [savedRuns, setSavedRuns] = useState<SavedRun[]>([])
  const [pendingSavedRun, setPendingSavedRun] = useState<SavedRun | null>(null)
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
    // Saved runs feed the Forms lane + run modal + Schedules picker; refresh after
    // the editor saves or a Config Sync pull lands new ones.
    const loadSavedRuns = () => bridge.get_saved_runs().then(setSavedRuns).catch(() => {})
    loadSavedRuns()
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
    // Context-window gauge: each chat turn reports its prompt size + composition.
    // A cleared chat resets the gauge so it doesn't show a stale fill.
    const onContextEstimate = (e: Event) => {
      const d = (e as CustomEvent).detail as ContextEstimate | undefined
      if (d && d.total_tokens > 0) setCtx(d)
    }
    const onChatCleared = () => setCtx(null)
    // "Compact now" drops messages but there's no fresh model round-trip to
    // recompute the exact gauge until the next turn — so estimate the drop
    // immediately by scaling the History bucket (and the total) by the share of
    // messages removed, instead of leaving a stale number. The next turn's
    // context_estimate replaces this with the exact figure.
    const onCompacted = (e: Event) => {
      const { dropped } = (e as CustomEvent).detail as { dropped?: number }
      const n = dropped ?? 0
      if (n <= 0) return
      setCtx(prev => {
        if (!prev) return prev
        const msgs = prev.history_msgs ?? 0
        if (msgs <= 0) return prev
        const kept = Math.max(0, msgs - n)
        const ratio = kept / msgs
        const hist = prev.buckets.find(b => b.key === 'history')
        const histTokens = hist?.tokens ?? 0
        const removed = Math.round(histTokens * (1 - ratio))
        if (removed <= 0) return { ...prev, history_msgs: kept }
        return {
          ...prev,
          total_tokens: Math.max(0, prev.total_tokens - removed),
          history_msgs: kept,
          buckets: prev.buckets.map(b =>
            b.key === 'history' ? { ...b, tokens: Math.max(0, b.tokens - removed), est: true } : b
          ),
        }
      })
    }
    window.addEventListener('runspec:context_estimate', onContextEstimate)
    window.addEventListener('runspec:chat_cleared', onChatCleared)
    window.addEventListener('runspec:chat_compacted', onCompacted)
    window.addEventListener('runspec:hosts_updated', onHostsUpdated)
    window.addEventListener('runspec:hosts_updated', loadProfiles)
    window.addEventListener('runspec:runnables_updated', onRunnablesUpdated)
    window.addEventListener('runspec:runbooks_changed', loadRunbooks)
    window.addEventListener('runspec:saved_runs_changed', loadSavedRuns)
    window.addEventListener('runspec:discovery_state', onDiscoveryState)
    window.addEventListener('runspec:room_connection', loadRooms)
    return () => {
      window.removeEventListener('runspec:hosts_updated', onHostsUpdated)
      window.removeEventListener('runspec:hosts_updated', loadProfiles)
      window.removeEventListener('runspec:runnables_updated', onRunnablesUpdated)
      window.removeEventListener('runspec:runbooks_changed', loadRunbooks)
      window.removeEventListener('runspec:saved_runs_changed', loadSavedRuns)
      window.removeEventListener('runspec:discovery_state', onDiscoveryState)
      window.removeEventListener('runspec:room_connection', loadRooms)
      window.removeEventListener('runspec:context_estimate', onContextEstimate)
      window.removeEventListener('runspec:chat_cleared', onChatCleared)
      window.removeEventListener('runspec:chat_compacted', onCompacted)
    }
  }, [])

  const toggleTheme = () => {
    const next = !isDark
    setIsDark(next)
    localStorage.setItem('theme', next ? 'dark' : 'light')
  }

  // Live-compaction lever from the context gauge: condense the older history so
  // the next turn carries less context, without losing the thread.
  const compactChat = () => {
    bridge.compact_chat().then(r => {
      if (r.compacted > 0) message.success(`Compacted ${r.compacted} older message${r.compacted === 1 ? '' : 's'} out of context.`)
      else message.info('Nothing to compact yet — the conversation is still short.')
    }).catch(() => message.error('Compaction failed.'))
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

  // A saved run with open params opens the params-only dialog; one with none
  // runs immediately (the "runs immediately" card affordance).
  const handleOpenSavedRun = (savedRun: SavedRun) => {
    if ((savedRun.params ?? []).length === 0 && !savedRun.argsError) {
      handleRunSavedRun(savedRun.id, {})
      return
    }
    setPendingSavedRun(savedRun)
  }

  // The run modal collected values; hand off to ConsoleView (it owns the
  // bridge.run_saved_run call + one run block per target host).
  const handleRunSavedRun = (id: string, values: Record<string, unknown>) => {
    setInputHistory(h => [...h, `»${id}`])
    setView('console')
    window.dispatchEvent(new CustomEvent('runspec:run_saved_run', { detail: { id, values } }))
  }

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
  const titleCol  = brandTheme.accent_color || (isDark ? DEFAULT_ACCENT.dark : DEFAULT_ACCENT.light)
  const contentBg = isDark ? '#111'    : '#ffffff'
  const iconCol   = textMuted(isDark)
  const textCol   = isDark ? '#ccc'    : '#333'

  // Stamp the theme + accent onto the document root so index.css (scrollbars,
  // hover fills, focus rings) stays in sync with the active theme and any
  // white-label accent — pure CSS, no per-element JS.
  useEffect(() => {
    document.documentElement.dataset.theme = isDark ? 'dark' : 'light'
    document.documentElement.style.setProperty('--rs-accent', titleCol)
    // Mirror the muted-text tokens as CSS vars for call sites without isDark
    // in scope (inline `var(--rs-text-*)` styles) — same source of truth.
    document.documentElement.style.setProperty('--rs-text-secondary', textSecondary(isDark))
    document.documentElement.style.setProperty('--rs-text-muted', textMuted(isDark))
    document.documentElement.style.setProperty('--rs-text-faint', textFaint(isDark))
  }, [isDark, titleCol])

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
      <AccentContext.Provider value={titleCol}>
      <ConfigProvider theme={{
          algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
          token: {
            // Two-font system: UI font for chrome (nav, labels, buttons, prose);
            // values, args, host names, logs and the command bar stay monospace
            // via explicit fontFamily where it earns its keep.
            fontFamily: UI_FONT,
            // White-label accent → drives buttons, links, active tabs, switches.
            ...(brandTheme.accent_color ? { colorPrimary: brandTheme.accent_color } : {}),
          },
        }}>
        {/* White-label content watermark — style-aware (tiled / banner / center),
            a passthrough when no watermark text/logo is set. */}
        <BrandWatermark theme={brandTheme} isDark={isDark}>
        <div style={{ height: '100vh', display: 'flex', flexDirection: 'column', overflow: 'hidden', background: contentBg, fontFamily: UI_FONT }}>
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
              fontFamily: 'monospace',  // wordmark keeps the terminal identity
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
                      className="rs-host-row"
                      onClick={() => handleHostSelect(h.name)}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 8,
                        padding: '8px 12px 8px 14px', cursor: 'pointer',
                        borderLeft: selectedHost === h.name
                          ? `2px solid ${titleCol}`
                          : '2px solid transparent',
                        // Selected fill is inline (wins over the CSS :hover rule);
                        // unselected rows leave background to .rs-host-row:hover.
                        background: selectedHost === h.name
                          ? (isDark ? 'rgba(79,193,255,0.08)' : 'rgba(9,88,217,0.06)')
                          : undefined,
                      }}
                    >
                      <span style={{ fontSize: 9, lineHeight: 1, color: h.connected ? '#52c41a' : '#595959' }}>●</span>
                      <span style={{
                        fontSize: 13, color: textCol, flex: 1, fontFamily: 'monospace',
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
                            className="rs-group-header"
                            onClick={() => toggleGroup(g)}
                            style={{
                              display: 'flex', alignItems: 'center', gap: 6,
                              padding: '10px 14px 3px',
                              fontSize: 10, color: textSecondary(isDark),
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
                // The selector connects to one room at a time, so the footer
                // reflects the *active* room's health — not a count of every
                // configured room. A non-selected host is a standby (grey, calm),
                // and "no room selected" is detached, not a failure.
                const GREEN = '#52c41a', RED = '#ff4d4f', GREY = '#8c8c8c'
                const active = rooms.find(r => r.active)
                const activeUp = !!active?.connected
                const roomState = (r: RoomInfo) =>
                  r.active ? (r.connected ? 'connected' : 'connecting') : 'standby'
                const stateColor = (r: RoomInfo) =>
                  r.active ? (r.connected ? GREEN : RED) : GREY
                // Dot: green = active & up; red = active but not reachable;
                // grey = detached (nothing selected).
                const dot = !active ? GREY : (activeUp ? GREEN : RED)
                const label = !active
                  ? 'no room'
                  : `${active.queue || active.room}${activeUp ? '' : ' · connecting'}`
                return (
                  <Popover
                    placement="topLeft"
                    mouseEnterDelay={0.2}
                    content={
                      <div style={{ maxWidth: 320, maxHeight: 320, overflowY: 'auto', fontSize: 12 }}>
                        <div style={{ marginBottom: 6, color: iconCol }}>
                          {!active
                            ? 'No room selected — this console is detached'
                            : `Connected to ${active.queue || active.room} · ${active.host}`}
                          {rooms.length > 1 && <> · {rooms.length} configured</>}
                        </div>
                        {rooms.map(r => {
                          const st = roomState(r)
                          return (
                            <div key={`${r.host}/${r.room}`} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 4 }}>
                              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                <span style={{ color: stateColor(r), marginRight: 6 }}>●</span>
                                <span style={{ color: titleCol }}>{r.queue || r.room}</span>
                                <span style={{ color: iconCol }}> · {r.host}</span>
                              </span>
                              <span style={{ color: stateColor(r), flexShrink: 0 }}>{st}</span>
                            </div>
                          )
                        })}
                        {active && !activeUp && (
                          <div style={{ marginTop: 6, color: iconCol, fontSize: 11 }}>
                            The active room’s server isn’t reachable on {active.host} — check the room process is running on its port.
                          </div>
                        )}
                        {rooms.length > 1 && (
                          <div style={{ marginTop: 6, color: iconCol, fontSize: 11 }}>
                            Standby hosts run the same room for redundancy; switch the active one in Settings → Self-Service.
                          </div>
                        )}
                      </div>
                    }
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 5, cursor: 'default' }}>
                      <CommentOutlined className={active && !activeUp ? 'rs-pulse' : undefined} style={{ fontSize: 11, color: dot }} />
                      <span style={{ color: titleCol, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 160 }}>
                        {label}
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

              {/* Context-window gauge: the active chat's latest-turn fill + a
                  by-source composition hover. Hidden until a chat reports usage,
                  so non-chat tabs don't show a stale/zero gauge. See
                  docs/design/console-context-indicator.md. */}
              {ctx && (() => {
                const pct = ctx.window > 0 ? Math.round((ctx.total_tokens / ctx.window) * 100) : 0
                const dot = pct >= 85 ? '#ff4d4f' : (pct >= 60 ? '#faad14' : '#52c41a')
                const shown = ctx.buckets.filter(b => b.tokens > 0).slice().sort((a, b) => b.tokens - a.tokens)
                const tok = (k: string) => ctx.buckets.find(b => b.key === k)?.tokens ?? 0
                const pins = ctx.memory_pins ?? 0
                const pinsShown = ctx.memory_pins_shown ?? 6
                // Lever nudges (Phase 2): suggest search mode when tool/catalog
                // definitions dominate, offer compaction when history is long, and
                // flag a pin set larger than what folds into the prompt.
                const toolShare = ctx.total_tokens > 0 ? (tok('tools') + tok('catalog')) / ctx.total_tokens : 0
                const nudgeSearch = (ctx.tool_mode && ctx.tool_mode !== 'search') ? toolShare >= 0.30 : false
                const canCompact = (ctx.history_msgs ?? 0) >= 24
                const pinsOverflow = pins > pinsShown
                const hasTips = nudgeSearch || canCompact || pinsOverflow
                return (
                  <Popover
                    placement="topLeft"
                    mouseEnterDelay={0.2}
                    content={
                      <div style={{ maxWidth: 320, fontSize: 12 }}>
                        <div style={{ marginBottom: 6, color: iconCol }}>
                          <span style={{ color: titleCol, fontVariantNumeric: 'tabular-nums' }}>{ctx.total_tokens.toLocaleString()}</span>
                          {' '}of {ctx.window.toLocaleString()} tokens ({pct}%)
                          {ctx.cache_hit_pct > 0 && <span> · {ctx.cache_hit_pct}% cached</span>}
                        </div>
                        {shown.map(b => (
                          <div key={b.key} style={{ marginBottom: 4 }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                              <span style={{ color: titleCol }}>
                                {CTX_BUCKET_LABELS[b.key] || b.key}
                                {b.key === 'memory' && pins > 0 && <span style={{ color: iconCol }}> · {pins} pinned</span>}
                                {b.key === 'history' && (ctx.history_msgs ?? 0) > 0 && <span style={{ color: iconCol }}> · {ctx.history_msgs} msgs</span>}
                              </span>
                              <span style={{ color: iconCol, fontVariantNumeric: 'tabular-nums' }}>{fmtTokens(b.tokens)}</span>
                            </div>
                            <div style={{ height: 4, background: borderCol, borderRadius: 2, marginTop: 2 }}>
                              <div style={{ height: 4, width: `${Math.round((b.tokens / ctx.total_tokens) * 100)}%`, background: dot, borderRadius: 2 }} />
                            </div>
                          </div>
                        ))}
                        {hasTips && (
                          <div style={{ marginTop: 8, paddingTop: 6, borderTop: `1px solid ${borderCol}`, display: 'flex', flexDirection: 'column', gap: 4 }}>
                            {nudgeSearch && (
                              <div style={{ color: iconCol }}>
                                Tool definitions are {Math.round(toolShare * 100)}% of the window.{' '}
                                <a onClick={() => setSettingsOpen(true)}>Switch to search mode</a> to replace them with a compact index.
                              </div>
                            )}
                            {canCompact && (
                              <div style={{ color: iconCol }}>
                                History is {ctx.history_msgs} messages.{' '}
                                <a onClick={compactChat}>Compact now</a> to condense older turns into a note.
                              </div>
                            )}
                            {pinsOverflow && (
                              <div style={{ color: iconCol }}>
                                {pins} pinned memory facts — only the top {pinsShown} fold into the prompt. Ask the agent to unpin stale ones.
                              </div>
                            )}
                          </div>
                        )}
                        <div style={{ marginTop: 6, color: iconCol, fontSize: 11 }}>
                          Total is exact (reported by the model); the per-section split is an estimate{ctx.model ? ` · ${ctx.model}` : ''}.
                        </div>
                      </div>
                    }
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 5, cursor: 'default' }}>
                      <DashboardOutlined style={{ fontSize: 11, color: dot }} />
                      <span>
                        <span style={{ fontVariantNumeric: 'tabular-nums', color: titleCol }}>{fmtTokens(ctx.total_tokens)}</span>
                        {' / '}{fmtTokens(ctx.window)}
                        <span style={{ color: dot }}> · {pct}%</span>
                      </span>
                    </div>
                  </Popover>
                )
              })()}

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
                  className="rs-nav-tab"
                  onClick={() => setView(tab.key)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '0 16px', fontSize: 13,
                    color: view === tab.key ? titleCol : iconCol,
                    borderBottom: view === tab.key ? `2px solid ${titleCol}` : '2px solid transparent',
                    transition: 'color 0.15s, background 0.15s',
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
                <Tooltip title="Transfer files (SFTP) — download, upload, fan-out, or relay between hosts">
                  <Button type="text" size="small" icon={<SwapOutlined />} onClick={() => setTransferOpen(true)} style={{ color: iconCol }} />
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
                <div className="rs-view" style={{ display: view === 'console' ? 'flex' : 'none', flexDirection: 'column', flex: 1, minHeight: 0 }}>
                  <ConsoleView inFlight={inFlight} pendingChat={pendingChat} onChatSent={() => setPendingChat(null)} />
                </div>
                {/* Triggers stays mounted (display toggle) so a pending approval's
                    Approve/Deny buttons survive tab switches — unmounting it drops
                    the live tool_confirm and the buttons can't return until timeout. */}
                <div className="rs-view" style={{ display: view === 'triggers' ? 'block' : 'none', flex: 1, overflow: 'auto' }}>
                  <TriggersView />
                </div>
                {/* History stays mounted (display toggle) so its fetched records
                    survive tab switches — returning shows the last data instantly
                    instead of unmounting and reloading from empty over SSH. Its
                    poll is gated on `active` so it only runs while visible. */}
                <div className="rs-view" style={{ display: view === 'history' ? 'block' : 'none', flex: 1, overflow: 'auto' }}>
                  <HistoryView search={historySearch} onSearchChange={setHistorySearch} onRerun={handleHistoryRerun} onAskLlm={handleAskLlm} activeScope={activeScope} onScopeToggle={handleScopeToggle} selectedHost={selectedHost} active={view === 'history'} />
                </div>
                {/* Logs stays mounted (display toggle) so its logs_status SSH
                    inventory survives tab switches; its fetch is gated on `active`
                    so it only runs while visible and on host change. */}
                <div className="rs-view" style={{ display: view === 'logs' ? 'block' : 'none', flex: 1, overflow: 'auto' }}>
                  <LogsView selectedHost={selectedHost} activeScope={activeScope} onScopeToggle={handleScopeToggle} active={view === 'logs'} />
                </div>
                {/* Analytics is lazy-loaded (heavy plots chunk). Mount it on first
                    visit, then keep it mounted (display toggle) so its scanned data
                    survives tab switches and it doesn't re-run the fleet scan on
                    every return; its poll is gated on `active` so it only runs
                    while visible. */}
                {analyticsVisited && (
                  <div className="rs-view" style={{ display: view === 'analytics' ? 'block' : 'none', flex: 1, overflow: 'auto' }}>
                    <Suspense fallback={<div style={{ display: 'flex', justifyContent: 'center', padding: 80 }}><Spin size="large" /></div>}>
                      <AnalyticsView selectedHost={selectedHost} activeScope={activeScope} active={view === 'analytics'} />
                    </Suspense>
                  </div>
                )}
                {view !== 'console' && view !== 'triggers' && view !== 'history' && view !== 'analytics' && view !== 'logs' && (
                  <div className="rs-view" style={{ flex: 1, overflow: 'auto' }}>
                    {view === 'specs'     && <SpecsView runnables={runnables} selectedHost={selectedHost} activeScope={activeScope} onScopeToggle={handleScopeToggle} />}
                    {view === 'forms'     && <FormsView runnables={runnables} hosts={hosts} selectedHost={selectedHost} activeScope={activeScope} onRunRunnable={handleRunRunnable} pendingForm={pendingForm} onPendingFormClear={() => setPendingForm(null)} runbooks={runbooks} onOpenRunbook={handleOpenRunbook} savedRuns={savedRuns} onOpenSavedRun={handleOpenSavedRun} />}
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
                    style={{ fontSize: 11, color: textMuted(isDark), padding: '0 4px', height: 'auto' }}
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
                  margin: '0 0 6px', fontSize: 11, color: textMuted(isDark),
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
        </BrandWatermark>

        <TransferModal
          open={transferOpen}
          onClose={() => setTransferOpen(false)}
          hosts={hosts}
          defaultRemoteHost={selectedHost}
        />

        <RunbookRunModal
          runbook={pendingRunbook}
          hostNames={hosts.map(h => h.name)}
          onClose={() => setPendingRunbook(null)}
          onRun={handleRunRunbook}
        />

        <SavedRunRunModal
          savedRun={pendingSavedRun}
          hostNames={hosts.map(h => h.name)}
          onClose={() => setPendingSavedRun(null)}
          onRun={handleRunSavedRun}
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
      </AccentContext.Provider>
    </ThemeContext.Provider>
  )
}

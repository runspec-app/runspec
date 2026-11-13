// Knowledge tab — the team knowledge wiki: an entity graph (hosts, runnables,
// files, jargon … linked by the facts about them), one page per entity, search,
// the review inbox where proposed facts wait for the operator, and pins (a few
// facts folded into every chat's context; replaced the old agent memory).
//
// Data comes from bridge.knowledge_* (bridge_knowledge.py). The same store backs
// the agent's wiki tools and the console MCP server, so facts proposed by the
// in-app agent or by Claude Code show up here in the inbox.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Badge, Button, Checkbox, Drawer, Dropdown, Empty, Form, Input, Modal, Popconfirm, Select, Space, Spin, Tabs,
  Tag, Tooltip, Typography, message,
} from 'antd'
import {
  ApartmentOutlined, BulbOutlined, CheckOutlined, CloseOutlined, DeleteOutlined, DownOutlined, EditOutlined, ExpandOutlined, FileSearchOutlined,
  CloseCircleOutlined, PlusOutlined, PushpinFilled, PushpinOutlined, ReloadOutlined,
  ZoomInOutlined, ZoomOutOutlined,
} from '@ant-design/icons'

import {
  bridge, type KnowledgeEdge, type KnowledgeFact, type KnowledgeFactInput,
  type KnowledgeNode, type KnowledgeOverview, type KnowledgePage, type Runnable,
} from '../bridge'
import { resolveScoped, shortcutHint, type ShortcutId } from '../shortcuts'
import {
  MONO_FONT, RADIUS, surfaces, textColor, textMuted, textSecondary,
  useIsDark, withAlpha,
} from '../ThemeContext'
import {
  fitView, forceLayout, neighbourhood, nodeRadius, parseAbout, splitEntity, typeColor,
  zoomAt, type Point, type View,
} from './knowledgeGraph'
import { KnowledgeSync, SyncResultBanner } from './KnowledgeSync'
import { KnowledgeTools } from './KnowledgeTools'
import { pageTargetFor } from './KnowledgeEdits'
import type { SyncResult } from '../components/MemorySyncTab'
import { When } from '../when'

const RANGE_OPTIONS = [
  { value: 7, label: 'Last 7 days' },
  { value: 30, label: 'Last 30 days' },
  { value: 90, label: 'Last 90 days' },
  { value: 0, label: 'All history' },
]

const rangeLabel = (days: number): string =>
  days ? `last ${days} days` : 'all history'

const { Text } = Typography

// Canvas size before the first measurement (and in tests, where nothing lays out).
const GRAPH_W = 960
const GRAPH_H = 620
const ZOOM_STEP = 1.25

/**
 * Runnable entities that aren't installed anywhere the console can see right
 * now (their usage is history). Null when discovery hasn't reported yet, so
 * nothing is dimmed on a cold start.
 */
export function staleCheck(installed: string[] | undefined): ((entity: string) => boolean) | null {
  if (!installed || installed.length === 0) return null
  const set = new Set(installed)
  return (entity: string) => {
    const [type, name] = splitEntity(entity)
    return type === 'runnable' && !set.has(name)
  }
}

interface KnowledgeViewProps {
  active: boolean
  aiEnabled: boolean
  // Opens Settings at a tab (Memory Sync's set-up button).
  onOpenSettings?: (tab: string) => void
  // Discovered runnables, for the Tools view's index and page headers.
  runnables?: Runnable[]
  // A tool page's actions: its run form, a prefilled question, its History.
  onRunTool?: (name: string) => void
  onAskAbout?: (name: string) => void
  onRecentRuns?: (name: string) => void
}

export function KnowledgeView({ active, aiEnabled, onOpenSettings, runnables = [], onRunTool, onAskAbout, onRecentRuns }: KnowledgeViewProps) {
  const isDark = useIsDark()
  const [overview, setOverview] = useState<KnowledgeOverview | null>(null)
  const [loading, setLoading] = useState(false)
  const [tab, setTab] = useState<'tools' | 'inbox'>('tools')
  // The fact graph opens over the tab (from a page's Show connections, an
  // entity tag, or the Fact graph button), not as a view of its own.
  const [graphOpen, setGraphOpen] = useState(false)
  const showGraph = (entity?: string) => {
    if (entity !== undefined) { setSelected(entity); setResults(null); setQuery('') }
    setGraphOpen(true)
  }
  const [syncResult, setSyncResult] = useState<SyncResult | null>(null)
  const [selected, setSelected] = useState<string>('')
  const [page, setPage] = useState<KnowledgePage | null>(null)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<KnowledgeFact[] | null>(null)
  // The query and results the side panel shows now, so a write can re-run the
  // search (a retired or edited fact must leave the list, not linger in it).
  const searchRef = useRef<{ query: string; open: boolean }>({ query: '', open: false })
  searchRef.current.open = results !== null
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [hideStale, setHideStale] = useState(false)
  const [busy, setBusy] = useState<'' | 'capture' | 'distill'>('')
  // How far back Capture and Suggest from chats read (days, 0 = all).
  // Starts at the range the last capture used, so the picker matches the graph.
  const [range, setRange] = useState<number | null>(null)
  const rangeDays = range ?? overview?.capture_days ?? 30
  const [editing, setEditing] = useState<{ fact?: KnowledgeFact; review?: boolean } | null>(null)
  // Ask the Tools view to open a page (from the fact graph's side panel).
  const [toolFocus, setToolFocus] = useState<{ target: string; n: number } | null>(null)
  const openWikiPage = (entity: string) => {
    setToolFocus(prev => ({ target: entity, n: (prev?.n ?? 0) + 1 }))
    setGraphOpen(false)
    setTab('tools')
  }

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setOverview(await bridge.knowledge_overview())
    } catch (e) {
      message.error(`Could not load the knowledge wiki: ${String(e)}`)
    } finally {
      setLoading(false)
    }
  }, [])

  const loadPage = useCallback(async (entity: string) => {
    if (!entity) { setPage(null); return }
    try { setPage(await bridge.knowledge_page(entity)) } catch { setPage(null) }
  }, [])

  useEffect(() => { if (active) refresh() }, [active, refresh])
  useEffect(() => { loadPage(selected) }, [selected, loadPage])

  // Re-run the open search, if any, after a write.
  const rerunSearch = useCallback(async () => {
    const { query: q, open } = searchRef.current
    if (!open || !q.trim()) return
    try {
      const fresh = await bridge.knowledge_search(q)
      if (searchRef.current.open && searchRef.current.query === q) setResults(fresh)
    } catch { /* keep what is shown */ }
  }, [])

  // The backend emits this after any write (agent, MCP client or this tab).
  useEffect(() => {
    const onUpdate = () => { refresh(); loadPage(selected); rerunSearch() }
    window.addEventListener('runspec:knowledge_updated', onUpdate)
    return () => window.removeEventListener('runspec:knowledge_updated', onUpdate)
  }, [refresh, loadPage, rerunSearch, selected])

  const runSearch = async (q: string) => {
    setQuery(q)
    searchRef.current.query = q
    if (!q.trim()) { setResults(null); return }
    try { setResults(await bridge.knowledge_search(q)) } catch { setResults([]) }
  }

  // After any write: the counts and graph, the open page, and the open search.
  const reloadAll = async () => {
    await Promise.all([refresh(), loadPage(selected), rerunSearch()])
  }

  const capture = async () => {
    setBusy('capture')
    try {
      const r = await bridge.knowledge_capture(rangeDays)
      if (r.ok) {
        const linked = r.relinked ? ` Linked ${r.relinked} earlier fact(s) to your hosts and groups.` : ''
        message.success(`Captured ${r.observed ?? 0} facts from ${r.runs ?? 0} runs (${rangeLabel(rangeDays)}) and your config.${linked}`)
      }
      else message.error(r.error || 'Capture failed.')
      await refresh()
    } finally { setBusy('') }
  }

  const distill = async () => {
    setBusy('distill')
    try {
      const r = await bridge.knowledge_distill(rangeDays)
      if (!r.ok) message.error(r.error || 'Could not suggest facts.')
      else if (r.note) message.info(r.note)
      else {
        const more = r.remaining ? ` ${r.remaining} more batch(es) to read: click again to continue.` : ''
        message.success(`Read ${r.conversations ?? 0} chat(s): ${r.proposed ?? 0} suggested fact(s) added to the inbox.${more}`, more ? 6 : 3)
      }
      if (r.ok && (r.proposed ?? 0) > 0) setTab('inbox')
      await refresh()
    } finally { setBusy('') }
  }

  const retire = async (fact: KnowledgeFact) => {
    const r = await bridge.knowledge_retire(fact.id)
    if (!r.ok) {
      message.error('That fact no longer exists.')
    } else {
      // Drop it from what is on screen straight away; the reload confirms it.
      setResults(prev => (prev ? prev.filter(f => f.id !== fact.id) : prev))
      setPage(prev => (prev ? { ...prev, facts: prev.facts.filter(f => f.id !== fact.id) } : prev))
      message.success('Fact retired.')
    }
    await reloadAll()
  }

  const pin = async (fact: KnowledgeFact, on: boolean) => {
    const r = await bridge.knowledge_pin(fact.id, on)
    if (!r.ok) message.error(r.error || 'Could not change the pin.')
    await refresh()
  }
  const pinnedIds = useMemo(() => new Set((overview?.pinned ?? []).map(f => f.id)), [overview])
  const pins = { ids: pinnedIds, max: overview?.pin_max ?? 6, onPin: pin }

  const review = async (fact: KnowledgeFact, approve: boolean) => {
    const r = await bridge.knowledge_review(fact.id, approve)
    if (!r.ok) message.error(r.error || 'Review failed.')
    await reloadAll()
  }

  const types = overview?.entity_types ?? []
  // One installed list for the whole tab: discovery (what the Tools view
  // uses) when it has run, else what the backend saw.
  const installed = useMemo(
    () => (runnables.length ? [...new Set(runnables.map(r => r.name))] : overview?.installed_runnables),
    [runnables, overview],
  )
  const isStale = useMemo(() => staleCheck(installed), [installed])
  const staleCount = useMemo(
    () => (isStale ? (overview?.graph.nodes ?? []).filter(n => isStale(n.id)).length : 0),
    [overview, isStale],
  )
  const shownNodes = useMemo(
    () => (overview?.graph.nodes ?? []).filter(n => !hidden.has(n.type) && !(hideStale && isStale?.(n.id))),
    [overview, hidden, hideStale, isStale],
  )
  const entitySuggestions = useMemo(
    () => (overview?.graph.nodes ?? []).map(n => n.id).sort(),
    [overview],
  )
  const inboxCount = overview?.inbox.length ?? 0
  const s = overview?.stats

  return (
    <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 12, height: '100%', boxSizing: 'border-box' }}>
      <Tabs
        activeKey={tab}
        onChange={k => setTab(k as 'tools' | 'inbox')}
        items={[
          { key: 'tools', label: 'Tools' },
          { key: 'inbox', label: <Badge count={inboxCount} size="small" offset={[8, -2]}>Inbox</Badge> },
        ]}
        tabBarExtraContent={
          <Space size={8}>
            <Tooltip title="Every fact drawn as a graph, with fact search and the facts pinned into the agent's context">
              <Button icon={<ApartmentOutlined />} onClick={() => showGraph()}>
                Fact graph{overview?.pinned?.length ? <span style={{ color: textMuted(isDark) }}> · {overview.pinned.length} pinned</span> : null}
              </Button>
            </Tooltip>
            <KnowledgeSync onOpenSettings={() => onOpenSettings?.('memorySync')} onResult={setSyncResult} />
          </Space>
        }
        style={{ marginBottom: -8 }}
      />
      <SyncResultBanner result={syncResult} onClose={() => setSyncResult(null)} />

      {tab === 'tools' ? (
        <KnowledgeTools runnables={runnables} active={active} onOpenSettings={onOpenSettings} focus={toolFocus}
          onRunTool={onRunTool} onAskAbout={aiEnabled ? onAskAbout : undefined} onRecentRuns={onRecentRuns}
          onShowConnections={showGraph} />
      ) : loading && !overview ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 80 }}><Spin /></div>
      ) : (
        <Inbox
          toolbar={
            <Space wrap>
              <Text style={{ color: textColor(isDark) }}>
                <b>{(overview?.inbox ?? []).length}</b> waiting for review
                {s ? <span style={{ color: textMuted(isDark) }}> · {s.approved} facts in the wiki</span> : null}
              </Text>
              <Dropdown trigger={['click']} menu={{
                items: [
                  { key: 'capture', icon: <ReloadOutlined />, label: <FindItem title="Capture usage" note="Run history and config on this console. No AI." /> },
                  ...(aiEnabled ? [{ key: 'distill', icon: <BulbOutlined />, label: <FindItem title="Suggest from chats" note="The model reads your saved chats; ones already read are skipped." /> }] : []),
                  { type: 'divider' as const },
                  { type: 'group' as const, label: 'Read the last', children: RANGE_OPTIONS.map(o => ({
                    key: `range:${o.value}`, label: o.label,
                    icon: o.value === rangeDays ? <CheckOutlined /> : <span style={{ display: 'inline-block', width: 14 }} />,
                  })) },
                ],
                onClick: ({ key }) => {
                  if (key === 'capture') capture()
                  else if (key === 'distill') distill()
                  else if (key.startsWith('range:')) setRange(Number(key.slice(6)))
                },
              }}>
                <Button icon={<FileSearchOutlined />} loading={busy === 'capture' || busy === 'distill'}>
                  Find facts <span style={{ color: textMuted(isDark) }}>· {rangeLabel(rangeDays)}</span> <DownOutlined />
                </Button>
              </Dropdown>
              <Button icon={<PlusOutlined />} onClick={() => setEditing({})}>Add fact</Button>
            </Space>
          }
          facts={overview?.inbox ?? []}
          onApprove={f => review(f, true)}
          onAddToPage={async f => {
            try {
              const r = await bridge.knowledge_draft_fact(f.id)
              if (r.ok) message.success(`Added to ${r.path} under \u201c${r.heading}\u201d. Propose it from Tools when you are ready.`)
              else message.error(r.error || 'Could not add it to a page.')
            } catch (e) { message.error(String(e)) }
            reloadAll()
          }}
          onReject={f => review(f, false)}
          onEdit={f => setEditing({ fact: f, review: true })}
          onEntity={e => showGraph(e)}
          pins={pins}
        />
      )}

      <Drawer open={graphOpen} onClose={() => setGraphOpen(false)} title="Fact graph" width="88%" destroyOnHidden
        styles={{ body: { display: 'flex', flexDirection: 'column', padding: 16 } }}>
        {!overview ? (
          <div style={{ display: 'flex', justifyContent: 'center', padding: 80 }}><Spin /></div>
        ) : (
        <div data-testid="fact-graph" style={{ display: 'flex', gap: 12, flex: 1, minHeight: 0 }}>
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
              <Input.Search
                allowClear
                placeholder="Search facts — hosts, files, jargon, tools…"
                style={{ maxWidth: 360 }}
                onSearch={runSearch}
                onChange={e => { if (!e.target.value) runSearch('') }}
              />
              <TypeFilter types={types} hidden={hidden} onToggle={t => setHidden(prev => {
                const next = new Set(prev)
                if (next.has(t)) next.delete(t)
                else next.add(t)
                return next
              })} />
              {staleCount > 0 && (
                <Tooltip title="Runnables with past usage that aren't installed on any connected host now (for example, removed from a package). Their usage is drawn faded.">
                  <Checkbox checked={hideStale} onChange={e => setHideStale(e.target.checked)} style={{ color: textSecondary(isDark) }}>
                    Hide not installed ({staleCount})
                  </Checkbox>
                </Tooltip>
              )}
            </div>
            {overview && overview.graph.nodes.length > 0 ? (
              <GraphCanvas
                nodes={shownNodes}
                edges={overview.graph.edges}
                isStale={isStale}
                selected={selected}
                onSelect={id => { setSelected(id); setResults(null); setQuery('') }}
                onClear={() => { setSelected(''); setResults(null); setQuery('') }}
              />
            ) : (
              <Empty
                style={{ marginTop: 60 }}
                description={
                  <span style={{ color: textSecondary(isDark) }}>
                    Nothing in the wiki yet. Capture from your run history and config, add a fact, or let the agent propose some as it works.
                  </span>
                }
              />
            )}
            {overview && (
              <Text style={{ color: textMuted(isDark), fontSize: 12 }}>
                Last captured {overview.captured_at ? <When t={overview.captured_at} /> : 'never'}{overview.capturing ? ' (capturing now…)' : ''} · stored in <span style={{ fontFamily: MONO_FONT }}>{overview.dir}</span>
              </Text>
            )}
          </div>
          <SidePanel
            query={query}
            results={results}
            page={page}
            isStale={isStale}
            onEntity={e => { setSelected(e); setResults(null) }}
            onEdit={f => setEditing({ fact: f })}
            onRetire={retire}
            onOpenWikiPage={openWikiPage}
            pinned={overview?.pinned ?? []}
            pins={pins}
          />
        </div>
        )}
      </Drawer>

      {editing && (
        <FactEditor
          fact={editing.fact}
          review={!!editing.review}
          kinds={overview?.kinds ?? []}
          suggestions={entitySuggestions}
          defaultAbout={editing.fact ? undefined : selected}
          onClose={() => setEditing(null)}
          onSaved={async () => { setEditing(null); await reloadAll() }}
        />
      )}
    </div>
  )
}

// ── type filter ──────────────────────────────────────────────────────────────

function TypeFilter({ types, hidden, onToggle }: { types: string[]; hidden: Set<string>; onToggle: (t: string) => void }) {
  const isDark = useIsDark()
  return (
    <Space size={4} wrap>
      {types.map(t => (
        <Tag.CheckableTag key={t} checked={!hidden.has(t)} onChange={() => onToggle(t)}
          style={{
            border: `1px solid ${hidden.has(t) ? surfaces(isDark).border : withAlpha(typeColor(t), 0.6)}`,
            background: hidden.has(t) ? 'transparent' : withAlpha(typeColor(t), 0.16),
            color: hidden.has(t) ? textMuted(isDark) : textColor(isDark),
          }}>
          <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 4, background: typeColor(t), marginRight: 6 }} />
          {t}
        </Tag.CheckableTag>
      ))}
    </Space>
  )
}

// ── graph ────────────────────────────────────────────────────────────────────

export function GraphCanvas({ nodes, edges, selected, onSelect, onClear, isStale = null }: {
  nodes: KnowledgeNode[]
  edges: KnowledgeEdge[]
  selected: string
  onSelect: (id: string) => void
  /** Clear the selection: a click on empty canvas, or the clear button. */
  onClear?: () => void
  isStale?: ((entity: string) => boolean) | null
}) {
  const isDark = useIsDark()
  const sf = surfaces(isDark)
  const [hover, setHover] = useState('')
  const [size, setSize] = useState({ w: GRAPH_W, h: GRAPH_H })
  const [view, setView] = useState<View>({ x: 0, y: 0, k: 1 })
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null)
  const boxRef = useRef<HTMLDivElement | null>(null)

  // Draw at the panel's real size (1 SVG unit = 1px), so the layout takes the
  // canvas's shape and labels stay a readable size. Rounded so a resize drag
  // doesn't re-run the layout on every pixel.
  useEffect(() => {
    const el = boxRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(entries => {
      const r = entries[0]?.contentRect
      if (!r || r.width < 50 || r.height < 50) return
      const w = Math.round(r.width / 20) * 20
      const h = Math.round(r.height / 20) * 20
      setSize(prev => (prev.w === w && prev.h === h ? prev : { w, h }))
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const ids = useMemo(() => new Set(nodes.map(n => n.id)), [nodes])
  const shownEdges = useMemo(() => edges.filter(e => ids.has(e.source) && ids.has(e.target)), [edges, ids])
  const pos = useMemo(() => forceLayout(nodes, shownEdges, size.w, size.h), [nodes, shownEdges, size])
  // Re-fit when the set of nodes or the canvas changes — not on every refresh
  // of the same graph, so the operator's zoom survives an inbox approval.
  const fitKey = useMemo(() => `${size.w}x${size.h}|${[...ids].sort().join(',')}`, [ids, size])
  const fit = useCallback(() => setView(fitView(pos, size.w, size.h)), [pos, size])
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { fit() }, [fitKey])

  const focus = hover || selected
  const near = useMemo(() => (focus ? neighbourhood(focus, shownEdges) : null), [focus, shownEdges])
  const maxW = Math.max(1, ...nodes.map(n => n.weight))
  const maxEdge = Math.max(1, ...shownEdges.map(e => e.weight))
  const labelSize = 12 / view.k // constant on screen whatever the zoom

  const onWheel = (e: React.WheelEvent) => {
    const rect = boxRef.current?.getBoundingClientRect()
    const sx = rect ? e.clientX - rect.left : size.w / 2
    const sy = rect ? e.clientY - rect.top : size.h / 2
    setView(v => zoomAt(v, e.deltaY < 0 ? 1.12 : 1 / 1.12, sx, sy))
  }
  const zoomBy = (f: number) => setView(v => zoomAt(v, f, size.w / 2, size.h / 2))

  return (
    <div ref={boxRef} style={{ position: 'relative', flex: 1, minHeight: 360, display: 'flex' }}>
      <svg
        role="img"
        aria-label="Knowledge graph"
        viewBox={`0 0 ${size.w} ${size.h}`}
        preserveAspectRatio="xMidYMid meet"
        style={{ width: '100%', height: '100%', position: 'absolute', inset: 0, background: sf.raised, border: `1px solid ${sf.border}`, borderRadius: RADIUS.md, cursor: drag.current ? 'grabbing' : 'grab', userSelect: 'none' }}
        onWheel={onWheel}
        onMouseDown={e => { drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y } }}
        onMouseMove={e => {
          const d = drag.current
          if (!d) return
          setView(v => ({ ...v, x: d.vx + (e.clientX - d.x), y: d.vy + (e.clientY - d.y) }))
        }}
        onMouseUp={e => {
          const d = drag.current
          drag.current = null
          // A click (not a pan) on empty canvas clears the selection.
          if (d && Math.hypot(e.clientX - d.x, e.clientY - d.y) < 4) onClear?.()
        }}
        onMouseLeave={() => { drag.current = null; setHover('') }}
      >
        <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
          {shownEdges.map(e => {
            const a = pos.get(e.source)
            const b = pos.get(e.target)
            if (!a || !b) return null
            const lit = near ? near.has(e.source) && near.has(e.target) && (e.source === focus || e.target === focus) : false
            const faded = !!isStale && (isStale(e.source) || isStale(e.target))
            return (
              <line key={`${e.source}|${e.target}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                stroke={lit ? textColor(isDark) : textMuted(isDark)}
                strokeOpacity={near ? (lit ? 0.9 : 0.08) : faded ? 0.15 : 0.35}
                strokeDasharray={faded ? '4 3' : undefined}
                strokeWidth={(0.8 + (e.weight / maxEdge) * 2.5) / Math.max(1, view.k)} />
            )
          })}
          {nodes.map(n => {
            const p: Point | undefined = pos.get(n.id)
            if (!p) return null
            const r = nodeRadius(n.weight)
            const stale = !!isStale?.(n.id)
            const dim = near ? !near.has(n.id) : false
            const isSel = n.id === selected
            const showLabel = isSel || n.id === hover || (near ? near.has(n.id) : n.weight >= maxW * 0.25 || nodes.length <= 60)
            return (
              <g key={n.id} data-testid={`node-${n.id}`} transform={`translate(${p.x} ${p.y})`} style={{ cursor: 'pointer' }}
                opacity={dim ? 0.18 : stale ? 0.4 : 1}
                onMouseDown={e => e.stopPropagation()}
                onMouseEnter={() => setHover(n.id)}
                onMouseLeave={() => setHover('')}
                onClick={() => onSelect(n.id)}>
                {isSel && <circle r={r + 5} fill="none" stroke={textColor(isDark)} strokeWidth={2} />}
                <circle r={r} fill={typeColor(n.type)} fillOpacity={n.pending || stale ? 0.35 : 0.9}
                  stroke={n.pending || stale ? typeColor(n.type) : sf.raised} strokeDasharray={n.pending || stale ? '3 2' : undefined} strokeWidth={1.5} />
                {showLabel && (
                  <text y={r + 4 + labelSize} textAnchor="middle" fontSize={labelSize} fontFamily={MONO_FONT} fill={textColor(isDark)}
                    stroke={sf.raised} strokeWidth={3 / view.k} paintOrder="stroke">
                    {n.name.length > 28 ? n.name.slice(0, 27) + '…' : n.name}
                  </text>
                )}
                <title>{`${n.id} · ${n.facts} fact(s) · weight ${n.weight}${stale ? ' · not installed on any connected host' : ''}`}</title>
              </g>
            )
          })}
        </g>
      </svg>
      <div style={{ position: 'absolute', top: 8, right: 8, display: 'flex', flexDirection: 'column', gap: 4 }}>
        <Tooltip title="Zoom in" placement="left"><Button size="small" icon={<ZoomInOutlined />} aria-label="Zoom in" onClick={() => zoomBy(ZOOM_STEP)} /></Tooltip>
        <Tooltip title="Zoom out" placement="left"><Button size="small" icon={<ZoomOutOutlined />} aria-label="Zoom out" onClick={() => zoomBy(1 / ZOOM_STEP)} /></Tooltip>
        <Tooltip title="Fit to view" placement="left"><Button size="small" icon={<ExpandOutlined />} aria-label="Fit to view" onClick={fit} /></Tooltip>
        {selected && onClear && (
          <Tooltip title="Clear selection (or click empty space)" placement="left">
            <Button size="small" icon={<CloseCircleOutlined />} aria-label="Clear selection" onClick={onClear} />
          </Tooltip>
        )}
      </div>
    </div>
  )
}

// ── side panel: search results or the selected entity's page ─────────────────

/** Which facts are pinned into every chat's context, and how to change that. */
export interface Pins {
  ids: Set<string>
  max: number
  onPin: (f: KnowledgeFact, on: boolean) => void
}

function SidePanel({ query, results, page, onEntity, onEdit, onRetire, onOpenWikiPage, pinned = [], pins, isStale = null }: {
  onOpenWikiPage?: (entity: string) => void
  query: string
  pinned?: KnowledgeFact[]
  pins?: Pins
  results: KnowledgeFact[] | null
  page: KnowledgePage | null
  isStale?: ((entity: string) => boolean) | null
  onEntity: (e: string) => void
  onEdit: (f: KnowledgeFact) => void
  onRetire: (f: KnowledgeFact) => void
}) {
  const isDark = useIsDark()
  const sf = surfaces(isDark)
  let body: React.ReactNode
  if (results) {
    body = (
      <>
        <div style={{ fontWeight: 600, color: textColor(isDark), marginBottom: 8 }}>
          {results.length} result(s) for “{query}”
        </div>
        {results.map(f => <FactRow key={f.id} fact={f} onEntity={onEntity} onEdit={onEdit} onRetire={onRetire} pins={pins} />)}
      </>
    )
  } else if (page && page.facts.length > 0) {
    const byKind = new Map<string, KnowledgeFact[]>()
    for (const f of page.facts) {
      byKind.set(f.kind, [...(byKind.get(f.kind) ?? []), f])
    }
    body = (
      <>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ width: 10, height: 10, borderRadius: 5, background: typeColor(page.type), display: 'inline-block' }} />
          <Text style={{ color: textMuted(isDark) }}>{page.type}</Text>
          {isStale?.(page.entity) && <Tag style={{ marginInlineEnd: 0 }}>not installed</Tag>}
          {onOpenWikiPage && /^(runnable|service|term|topic|team|runbook|host):/.test(page.entity) && (
            <a role="button" onClick={() => onOpenWikiPage(page.entity)} style={{ marginLeft: 'auto', fontSize: 12 }}>Open wiki page</a>
          )}
        </div>
        <div style={{ fontSize: 18, fontWeight: 600, fontFamily: MONO_FONT, color: textColor(isDark), wordBreak: 'break-all', margin: '2px 0 8px' }}>
          {page.name}
        </div>
        {[...byKind].map(([kind, facts]) => (
          <div key={kind} style={{ marginBottom: 12 }}>
            <div style={{ textTransform: 'uppercase', fontSize: 11, letterSpacing: 0.5, color: textMuted(isDark), marginBottom: 4 }}>{kind}</div>
            {facts.map(f => <FactRow key={f.id} fact={f} current={page.entity} onEntity={onEntity} onEdit={onEdit} onRetire={onRetire} pins={pins} />)}
          </div>
        ))}
        {page.links.length > 0 && (
          <div>
            <div style={{ textTransform: 'uppercase', fontSize: 11, letterSpacing: 0.5, color: textMuted(isDark), marginBottom: 4 }}>Linked</div>
            <Space size={[4, 4]} wrap>
              {page.links.map(l => <EntityTag key={l.entity} entity={l.entity} onClick={onEntity} />)}
            </Space>
          </div>
        )}
        {page.authors.length > 0 && (
          <div style={{ marginTop: 12, fontSize: 12, color: textMuted(isDark) }}>
            Contributors: {page.authors.join(', ')}
          </div>
        )}
      </>
    )
  } else {
    body = (
      <>
        {pinned.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ textTransform: 'uppercase', fontSize: 11, letterSpacing: 0.5, color: textMuted(isDark), marginBottom: 4 }}>
              Pinned · {pinned.length}/{pins?.max ?? 6} · in every chat's context
            </div>
            {pinned.map(f => <FactRow key={f.id} fact={f} onEntity={onEntity} pins={pins} />)}
          </div>
        )}
        <div style={{ color: textSecondary(isDark) }}>
          Click a node to open its page. Larger nodes have more confirmed facts; dashed
          ones only have facts waiting in the inbox. Drag to pan, scroll to zoom.
          Pin a fact to fold it into every chat's context.
        </div>
      </>
    )
  }
  return (
    <div style={{ width: 'clamp(360px, 34%, 560px)', flexShrink: 0, overflow: 'auto', background: sf.raised, border: `1px solid ${sf.border}`, borderRadius: RADIUS.md, padding: 14 }}>
      {body}
    </div>
  )
}

function EntityTag({ entity, onClick }: { entity: string; onClick?: (e: string) => void }) {
  const [type, name] = splitEntity(entity)
  const isDark = useIsDark()
  return (
    <Tag
      color={withAlpha(typeColor(type), 0.18)}
      style={{ color: textColor(isDark), cursor: onClick ? 'pointer' : 'default', fontFamily: MONO_FONT, borderColor: withAlpha(typeColor(type), 0.6), marginInlineEnd: 0 }}
      onClick={() => onClick?.(entity)}
    >
      {type}:{name}
    </Tag>
  )
}

function FactRow({ fact, current, onEntity, onEdit, onRetire, actions, pins }: {
  fact: KnowledgeFact
  pins?: Pins
  current?: string
  onEntity: (e: string) => void
  onEdit?: (f: KnowledgeFact) => void
  onRetire?: (f: KnowledgeFact) => void
  actions?: React.ReactNode
}) {
  const isDark = useIsDark()
  const sf = surfaces(isDark)
  return (
    <div style={{ padding: '8px 0', borderBottom: `1px solid ${sf.divider}` }}>
      <div style={{ color: textColor(isDark), lineHeight: 1.45 }}>{fact.text}</div>
      <Space size={[4, 4]} wrap style={{ marginTop: 6 }}>
        {fact.status !== 'approved' && (
          <Tag color={fact.status === 'pending' ? 'gold' : fact.status === 'observed' ? 'cyan' : 'default'}>{fact.status}</Tag>
        )}
        {fact.scope === 'local' && (
          <Tooltip title={`Stays on this console and is never shared with the team: ${fact.scope_reason || 'it is tied to this console'}.`}>
            <Tag data-testid="fact-scope-local" style={{ color: textSecondary(isDark), borderStyle: 'dashed', background: 'transparent' }}>This console</Tag>
          </Tooltip>
        )}
        {fact.about.filter(a => a !== current).map(a => <EntityTag key={a} entity={a} onClick={onEntity} />)}
      </Space>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
        <Text style={{ fontSize: 12, color: textMuted(isDark) }}>
          ×{fact.confirmations} · {fact.last_confirmed}{fact.source ? ` · ${fact.source}` : ''}{fact.author ? ` · ${fact.author}` : ''}
        </Text>
        <div style={{ flex: 1 }} />
        {actions}
        {pins && (fact.status === 'approved' || pins.ids.has(fact.id)) && (() => {
          const on = pins.ids.has(fact.id)
          const full = !on && pins.ids.size >= pins.max
          return (
            <Tooltip title={on ? 'Unpin' : full ? `${pins.max} facts already pinned; unpin one first` : "Pin into every chat's context"}>
              <Button
                size="small"
                type="text"
                disabled={full}
                icon={on ? <PushpinFilled /> : <PushpinOutlined />}
                aria-label={on ? 'Unpin fact' : 'Pin fact'}
                onClick={() => pins.onPin(fact, !on)}
              />
            </Tooltip>
          )
        })()}
        {onEdit && fact.status !== 'pending' && (
          <Tooltip title="Edit"><Button size="small" type="text" icon={<EditOutlined />} aria-label="Edit fact" onClick={() => onEdit(fact)} /></Tooltip>
        )}
        {onRetire && fact.status !== 'pending' && (
          <Popconfirm title="Retire this fact?" description="It stops showing and stops being returned to the agent." onConfirm={() => onRetire(fact)}>
            <Button size="small" type="text" icon={<DeleteOutlined />} aria-label="Retire fact" />
          </Popconfirm>
        )}
      </div>
    </div>
  )
}

// ── inbox ────────────────────────────────────────────────────────────────────

// One line in the Find facts menu: what it does, and what it reads.
function FindItem({ title, note }: { title: string; note: string }) {
  const isDark = useIsDark()
  return (
    <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.35, padding: '2px 0' }}>
      <span>{title}</span>
      <span style={{ fontSize: 12, color: textMuted(isDark) }}>{note}</span>
    </div>
  )
}

const INBOX_HINT_KEY = 'rs.knowledge.inboxHintSeen'

function Inbox({ facts, toolbar, onApprove, onAddToPage, onReject, onEdit, onEntity, pins }: {
  facts: KnowledgeFact[]
  toolbar?: React.ReactNode
  pins?: Pins
  onApprove: (f: KnowledgeFact) => void
  onAddToPage?: (f: KnowledgeFact) => void
  onReject: (f: KnowledgeFact) => void
  onEdit: (f: KnowledgeFact) => void
  onEntity: (e: string) => void
}) {
  const isDark = useIsDark()
  const sf = surfaces(isDark)
  const [cursor, setCursor] = useState(0)
  const [hint, setHint] = useState(() => { try { return !localStorage.getItem(INBOX_HINT_KEY) } catch { return true } })
  const dismissHint = () => { setHint(false); try { localStorage.setItem(INBOX_HINT_KEY, '1') } catch { /* private window */ } }
  const rowRefs = useRef<(HTMLDivElement | null)[]>([])
  const at = Math.min(cursor, Math.max(facts.length - 1, 0))

  // J/K move, P/A/E/R act on the highlighted fact (shortcuts.ts, scope 'inbox').
  const latest = useRef({ facts, at, onApprove, onAddToPage, onEdit })
  latest.current = { facts, at, onApprove, onAddToPage, onEdit }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing) return
      const id = resolveScoped(e, 'inbox')
      const { facts: fs, at: i, onApprove: approve, onAddToPage: add, onEdit: edit } = latest.current
      if (!id || fs.length === 0) return
      const f = fs[i]
      e.preventDefault()
      if (id === 'inboxNext') setCursor(Math.min(i + 1, fs.length - 1))
      else if (id === 'inboxPrev') setCursor(Math.max(i - 1, 0))
      else if (id === 'inboxApprove') approve(f)
      else if (id === 'inboxEdit') edit(f)
      else if (id === 'inboxAddToPage') { if (add && f.scope !== 'local' && pageTargetFor(f)) add(f) }
      else if (id === 'inboxReject') rowRefs.current[i]?.querySelector<HTMLElement>('[data-rs-reject]')?.click()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  useEffect(() => { rowRefs.current[at]?.scrollIntoView?.({ block: 'nearest' }) }, [at])

  const keys = (id: ShortcutId) => ` (${shortcutHint(id)})`
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, minHeight: 0, overflow: 'auto' }}>
      {toolbar}
      {hint && facts.length > 0 && (
        <Alert type="info" showIcon closable onClose={dismissHint} style={{ maxWidth: 900 }}
          message={<span style={{ display: 'block', maxWidth: '72ch' }}>
            Facts come from the agent, an MCP client such as Claude Code, or your chats. Nothing joins the wiki until you review it:
            {' '}<b>Add to page</b> puts it on its page in the team wiki, <b>Approve</b> keeps it as a fact.
            {' '}Keys: J and K move, then P, A, E or R.
          </span>} />
      )}
      {facts.length === 0 ? (
        <Empty style={{ marginTop: 40 }} description={<span style={{ color: textSecondary(isDark) }}>No facts waiting for review.</span>} />
      ) : (
    <div role="list" aria-label="Facts waiting for review" style={{ maxWidth: 900, background: sf.raised, border: `1px solid ${sf.border}`, borderRadius: RADIUS.md, padding: '4px 0' }}>
      {facts.map((f, i) => {
        const target = f.scope !== 'local' && onAddToPage ? pageTargetFor(f) : null
        const on = i === at
        return (
        <div key={f.id} role="listitem" ref={el => { rowRefs.current[i] = el }} aria-current={on ? 'true' : undefined}
          onClick={() => setCursor(i)}
          style={{ padding: '0 14px 0 11px', borderLeft: `3px solid ${on ? textMuted(isDark) : 'transparent'}` }}>
        <FactRow fact={f} onEntity={onEntity} pins={pins} actions={
          <Space size={2}>
            {/* One primary per row: Add to page when the fact has a page to go on, else Approve. */}
            {target && (
              <Tooltip title={`Approve it and add it to ${target.path} under \u201c${target.heading}\u201d, as an edit you propose later${keys('inboxAddToPage')}`}>
                <Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => onAddToPage!(f)}>Add to page</Button>
              </Tooltip>
            )}
            <Tooltip title={`${f.scope === 'local' ? 'Approve it as a fact on this console' : target ? 'Approve it as a fact, without adding it to a page' : 'Approve it as a fact'}${keys('inboxApprove')}`}>
              <Button size="small" type={target ? 'default' : 'primary'} icon={<CheckOutlined />} onClick={() => onApprove(f)}>Approve</Button>
            </Tooltip>
            <Tooltip title={`Edit, then approve${keys('inboxEdit')}`}>
              <Button size="small" type="text" icon={<EditOutlined />} onClick={() => onEdit(f)}>Edit</Button>
            </Tooltip>
            <Popconfirm title="Reject this fact?" description="It leaves the inbox and isn't added to the wiki." okText="Reject" okButtonProps={{ danger: true }} onConfirm={() => onReject(f)}>
              {/* Ant's danger red is ~2.8:1 on the gunmetal card; lift it for text. */}
              <Button size="small" type="text" danger data-rs-reject="" icon={<CloseOutlined />} style={isDark ? { color: DANGER_TEXT_DARK } : undefined}>Reject</Button>
            </Popconfirm>
          </Space>
        } />
        </div>
        )
      })}
    </div>
      )}
    </div>
  )
}

// Danger text that clears 4.5:1 on the dark card (Ant's #dc4446 is ~2.8:1).
const DANGER_TEXT_DARK = '#ff8f87'

// ── add / edit / edit-and-approve ────────────────────────────────────────────

function FactEditor({ fact, review, kinds, suggestions, defaultAbout, onClose, onSaved }: {
  fact?: KnowledgeFact
  review: boolean
  kinds: string[]
  suggestions: string[]
  defaultAbout?: string
  onClose: () => void
  onSaved: () => void
}) {
  const [kind, setKind] = useState(fact?.kind ?? 'note')
  const [about, setAbout] = useState<string[]>(fact?.about ?? (defaultAbout ? [defaultAbout] : []))
  const [text, setText] = useState(fact?.text ?? '')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const save = async () => {
    setSaving(true)
    setError('')
    try {
      const payload: KnowledgeFactInput = { kind, about: parseAbout(about.join(',')), text }
      const r = review && fact
        ? await bridge.knowledge_review(fact.id, true, payload)
        : await bridge.knowledge_save_fact(fact ? { ...payload, id: fact.id } : payload)
      if (r.ok) onSaved()
      else setError(r.error || 'Could not save.')
    } finally { setSaving(false) }
  }

  return (
    <Modal
      open
      title={review ? 'Edit and approve' : fact ? 'Edit fact' : 'Add a fact'}
      okText={review ? 'Approve' : 'Save'}
      onOk={save}
      confirmLoading={saving}
      okButtonProps={{ disabled: !text.trim() || about.length === 0 }}
      onCancel={onClose}
    >
      <Form layout="vertical">
        <Form.Item label="Kind">
          <Select value={kind} onChange={setKind} options={kinds.map(k => ({ value: k, label: k }))} />
        </Form.Item>
        <Form.Item label="About" help="Entities as type:name, e.g. runnable:tail-log, app:orders, service:lb, term:BAU. A fact about a host, group or profile stays on this console.">
          <Select mode="tags" value={about} onChange={setAbout} tokenSeparators={[',']}
            options={suggestions.map(s => ({ value: s, label: s }))} placeholder="runnable:tail-log" />
        </Form.Item>
        <Form.Item label="Fact" validateStatus={error ? 'error' : undefined} help={error || 'One or two sentences that hold on any teammate\'s console. No secrets, home directories or host names; those keep a fact on this console.'}>
          <Input.TextArea rows={3} value={text} onChange={e => setText(e.target.value)} />
        </Form.Item>
      </Form>
    </Modal>
  )
}

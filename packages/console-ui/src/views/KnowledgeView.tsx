// Knowledge tab — the team knowledge wiki: an entity graph (hosts, runnables,
// files, jargon … linked by the facts about them), one page per entity, search,
// and the review inbox where proposed facts wait for the operator.
//
// Data comes from bridge.knowledge_* (bridge_knowledge.py). The same store backs
// the agent's wiki tools and the console MCP server, so facts proposed by the
// in-app agent or by Claude Code show up here in the inbox.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Badge, Button, Empty, Form, Input, Modal, Popconfirm, Select, Space, Spin, Tabs,
  Tag, Tooltip, Typography, message,
} from 'antd'
import {
  BulbOutlined, CheckOutlined, CloseOutlined, DeleteOutlined, EditOutlined,
  PlusOutlined, ReloadOutlined,
} from '@ant-design/icons'

import {
  bridge, type KnowledgeEdge, type KnowledgeFact, type KnowledgeFactInput,
  type KnowledgeNode, type KnowledgeOverview, type KnowledgePage,
} from '../bridge'
import {
  MONO_FONT, RADIUS, surfaces, textColor, textMuted, textSecondary, useAccent,
  useIsDark, withAlpha,
} from '../ThemeContext'
import {
  forceLayout, neighbourhood, nodeRadius, parseAbout, splitEntity, typeColor,
} from './knowledgeGraph'

const { Text } = Typography

const GRAPH_W = 960
const GRAPH_H = 620

interface KnowledgeViewProps {
  active: boolean
  aiEnabled: boolean
}

export function KnowledgeView({ active, aiEnabled }: KnowledgeViewProps) {
  const isDark = useIsDark()
  const [overview, setOverview] = useState<KnowledgeOverview | null>(null)
  const [loading, setLoading] = useState(false)
  const [tab, setTab] = useState<'graph' | 'inbox'>('graph')
  const [selected, setSelected] = useState<string>('')
  const [page, setPage] = useState<KnowledgePage | null>(null)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<KnowledgeFact[] | null>(null)
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState<'' | 'capture' | 'distill'>('')
  const [editing, setEditing] = useState<{ fact?: KnowledgeFact; review?: boolean } | null>(null)

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

  // The backend emits this after any write (agent, MCP client or this tab).
  useEffect(() => {
    const onUpdate = () => { refresh(); loadPage(selected) }
    window.addEventListener('runspec:knowledge_updated', onUpdate)
    return () => window.removeEventListener('runspec:knowledge_updated', onUpdate)
  }, [refresh, loadPage, selected])

  const runSearch = async (q: string) => {
    setQuery(q)
    if (!q.trim()) { setResults(null); return }
    try { setResults(await bridge.knowledge_search(q)) } catch { setResults([]) }
  }

  const capture = async () => {
    setBusy('capture')
    try {
      const r = await bridge.knowledge_capture()
      if (r.ok) message.success(`Captured ${r.observed ?? 0} usage patterns from ${r.runs ?? 0} runs.`)
      else message.error(r.error || 'Capture failed.')
      await refresh()
    } finally { setBusy('') }
  }

  const distill = async () => {
    setBusy('distill')
    try {
      const r = await bridge.knowledge_distill()
      if (!r.ok) message.error(r.error || 'Could not suggest facts.')
      else if (r.note) message.info(r.note)
      else message.success(`${r.proposed ?? 0} suggested fact(s) added to the inbox.`)
      if (r.ok && (r.proposed ?? 0) > 0) setTab('inbox')
      await refresh()
    } finally { setBusy('') }
  }

  const retire = async (fact: KnowledgeFact) => {
    await bridge.knowledge_retire(fact.id)
    message.success('Fact retired.')
    await refresh()
    await loadPage(selected)
  }

  const review = async (fact: KnowledgeFact, approve: boolean) => {
    const r = await bridge.knowledge_review(fact.id, approve)
    if (!r.ok) message.error(r.error || 'Review failed.')
    await refresh()
    await loadPage(selected)
  }

  const types = overview?.entity_types ?? []
  const entitySuggestions = useMemo(
    () => (overview?.graph.nodes ?? []).map(n => n.id).sort(),
    [overview],
  )
  const inboxCount = overview?.inbox.length ?? 0
  const s = overview?.stats

  return (
    <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 12, height: '100%', boxSizing: 'border-box' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <Input.Search
          allowClear
          placeholder="Search the wiki — hosts, files, jargon, tools…"
          style={{ maxWidth: 420 }}
          onSearch={runSearch}
          onChange={e => { if (!e.target.value) runSearch('') }}
        />
        <Text style={{ color: textSecondary(isDark) }}>
          {s ? `${s.entities} entities · ${s.approved} facts · ${s.observed} observed` : ''}
        </Text>
        <div style={{ flex: 1 }} />
        <Space wrap>
          <Tooltip title="Mine recent run history for usage patterns: common args, where runnables run, what runs after what. No AI involved.">
            <Button icon={<ReloadOutlined />} loading={busy === 'capture'} onClick={capture}>Capture usage</Button>
          </Tooltip>
          {aiEnabled && (
            <Tooltip title="Ask the model to suggest facts from the last week of chats. Suggestions go to the inbox for you to review.">
              <Button icon={<BulbOutlined />} loading={busy === 'distill'} onClick={distill}>Suggest from chats</Button>
            </Tooltip>
          )}
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setEditing({})}>Add fact</Button>
        </Space>
      </div>

      <Tabs
        activeKey={tab}
        onChange={k => setTab(k as 'graph' | 'inbox')}
        items={[
          { key: 'graph', label: 'Graph' },
          { key: 'inbox', label: <Badge count={inboxCount} size="small" offset={[8, -2]}>Inbox</Badge> },
        ]}
        style={{ marginBottom: -8 }}
      />

      {loading && !overview ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 80 }}><Spin /></div>
      ) : tab === 'inbox' ? (
        <Inbox
          facts={overview?.inbox ?? []}
          onApprove={f => review(f, true)}
          onReject={f => review(f, false)}
          onEdit={f => setEditing({ fact: f, review: true })}
          onEntity={e => { setSelected(e); setTab('graph') }}
        />
      ) : (
        <div style={{ display: 'flex', gap: 12, flex: 1, minHeight: 0 }}>
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
            <TypeFilter types={types} hidden={hidden} onToggle={t => setHidden(prev => {
              const next = new Set(prev)
              if (next.has(t)) next.delete(t)
              else next.add(t)
              return next
            })} />
            {overview && overview.graph.nodes.length > 0 ? (
              <GraphCanvas
                nodes={overview.graph.nodes.filter(n => !hidden.has(n.type))}
                edges={overview.graph.edges}
                selected={selected}
                onSelect={id => { setSelected(id); setResults(null); setQuery('') }}
              />
            ) : (
              <Empty
                style={{ marginTop: 60 }}
                description={
                  <span style={{ color: textSecondary(isDark) }}>
                    Nothing in the wiki yet. Capture usage from your run history, add a fact, or let the agent propose some as it works.
                  </span>
                }
              />
            )}
            {overview && (
              <Text style={{ color: textMuted(isDark), fontSize: 12 }}>
                Usage last captured {overview.captured_at ? new Date(overview.captured_at).toLocaleString() : 'never'} · stored in <span style={{ fontFamily: MONO_FONT }}>{overview.dir}</span>
              </Text>
            )}
          </div>
          <SidePanel
            query={query}
            results={results}
            page={page}
            onEntity={e => { setSelected(e); setResults(null) }}
            onEdit={f => setEditing({ fact: f })}
            onRetire={retire}
          />
        </div>
      )}

      {editing && (
        <FactEditor
          fact={editing.fact}
          review={!!editing.review}
          kinds={overview?.kinds ?? []}
          suggestions={entitySuggestions}
          defaultAbout={editing.fact ? undefined : selected}
          onClose={() => setEditing(null)}
          onSaved={async () => { setEditing(null); await refresh(); await loadPage(selected) }}
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
          style={{ border: `1px solid ${surfaces(isDark).border}`, color: textColor(isDark) }}>
          <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 4, background: typeColor(t), marginRight: 6 }} />
          {t}
        </Tag.CheckableTag>
      ))}
    </Space>
  )
}

// ── graph ────────────────────────────────────────────────────────────────────

export function GraphCanvas({ nodes, edges, selected, onSelect }: {
  nodes: KnowledgeNode[]
  edges: KnowledgeEdge[]
  selected: string
  onSelect: (id: string) => void
}) {
  const isDark = useIsDark()
  const accent = useAccent()
  const sf = surfaces(isDark)
  const [hover, setHover] = useState('')
  const [view, setView] = useState({ x: 0, y: 0, k: 1 })
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null)
  const svgRef = useRef<SVGSVGElement | null>(null)

  const ids = useMemo(() => new Set(nodes.map(n => n.id)), [nodes])
  const shownEdges = useMemo(() => edges.filter(e => ids.has(e.source) && ids.has(e.target)), [edges, ids])
  const pos = useMemo(() => forceLayout(nodes, shownEdges, GRAPH_W, GRAPH_H), [nodes, shownEdges])
  const focus = hover || selected
  const near = useMemo(() => (focus ? neighbourhood(focus, shownEdges) : null), [focus, shownEdges])
  const maxW = Math.max(1, ...nodes.map(n => n.weight))
  const maxEdge = Math.max(1, ...shownEdges.map(e => e.weight))

  const onWheel = (e: React.WheelEvent) => {
    const k = Math.min(4, Math.max(0.4, view.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12)))
    setView(v => ({ ...v, k }))
  }

  return (
    <svg
      ref={svgRef}
      role="img"
      aria-label="Knowledge graph"
      viewBox={`0 0 ${GRAPH_W} ${GRAPH_H}`}
      style={{ width: '100%', flex: 1, minHeight: 360, background: sf.raised, border: `1px solid ${sf.border}`, borderRadius: RADIUS.md, cursor: drag.current ? 'grabbing' : 'grab', userSelect: 'none' }}
      onWheel={onWheel}
      onMouseDown={e => { drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y } }}
      onMouseMove={e => {
        const d = drag.current
        if (!d) return
        const rect = svgRef.current?.getBoundingClientRect()
        const scale = rect && rect.width > 0 ? GRAPH_W / rect.width : 1
        setView(v => ({ ...v, x: d.vx + (e.clientX - d.x) * scale, y: d.vy + (e.clientY - d.y) * scale }))
      }}
      onMouseUp={() => { drag.current = null }}
      onMouseLeave={() => { drag.current = null; setHover('') }}
    >
      <g transform={`translate(${view.x + (GRAPH_W / 2) * (1 - view.k)} ${view.y + (GRAPH_H / 2) * (1 - view.k)}) scale(${view.k})`}>
        {shownEdges.map(e => {
          const a = pos.get(e.source)
          const b = pos.get(e.target)
          if (!a || !b) return null
          const lit = near ? near.has(e.source) && near.has(e.target) && (e.source === focus || e.target === focus) : false
          return (
            <line key={`${e.source}|${e.target}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
              stroke={lit ? accent : textMuted(isDark)}
              strokeOpacity={near ? (lit ? 0.9 : 0.08) : 0.35}
              strokeWidth={0.8 + (e.weight / maxEdge) * 2.5} />
          )
        })}
        {nodes.map(n => {
          const p = pos.get(n.id)
          if (!p) return null
          const r = nodeRadius(n.weight)
          const dim = near ? !near.has(n.id) : false
          const isSel = n.id === selected
          const showLabel = isSel || n.id === hover || (near ? near.has(n.id) : n.weight >= maxW * 0.25 || nodes.length <= 40)
          return (
            <g key={n.id} data-testid={`node-${n.id}`} transform={`translate(${p.x} ${p.y})`} style={{ cursor: 'pointer' }}
              opacity={dim ? 0.18 : 1}
              onMouseDown={e => e.stopPropagation()}
              onMouseEnter={() => setHover(n.id)}
              onMouseLeave={() => setHover('')}
              onClick={() => onSelect(n.id)}>
              {isSel && <circle r={r + 5} fill="none" stroke={accent} strokeWidth={2} />}
              <circle r={r} fill={typeColor(n.type)} fillOpacity={n.pending ? 0.35 : 0.9}
                stroke={n.pending ? typeColor(n.type) : sf.raised} strokeDasharray={n.pending ? '3 2' : undefined} strokeWidth={1.5} />
              {showLabel && (
                <text y={r + 13} textAnchor="middle" fontSize={11} fontFamily={MONO_FONT} fill={textColor(isDark)}
                  stroke={sf.raised} strokeWidth={3} paintOrder="stroke">
                  {n.name.length > 28 ? n.name.slice(0, 27) + '…' : n.name}
                </text>
              )}
              <title>{`${n.id} · ${n.facts} fact(s) · weight ${n.weight}`}</title>
            </g>
          )
        })}
      </g>
    </svg>
  )
}

// ── side panel: search results or the selected entity's page ─────────────────

function SidePanel({ query, results, page, onEntity, onEdit, onRetire }: {
  query: string
  results: KnowledgeFact[] | null
  page: KnowledgePage | null
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
        {results.map(f => <FactRow key={f.id} fact={f} onEntity={onEntity} onEdit={onEdit} onRetire={onRetire} />)}
      </>
    )
  } else if (page && page.facts.length > 0) {
    const byKind = new Map<string, KnowledgeFact[]>()
    for (const f of page.facts) byKind.set(f.kind, [...(byKind.get(f.kind) ?? []), f])
    body = (
      <>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ width: 10, height: 10, borderRadius: 5, background: typeColor(page.type), display: 'inline-block' }} />
          <Text style={{ color: textMuted(isDark) }}>{page.type}</Text>
        </div>
        <div style={{ fontSize: 18, fontWeight: 600, fontFamily: MONO_FONT, color: textColor(isDark), wordBreak: 'break-all', margin: '2px 0 8px' }}>
          {page.name}
        </div>
        {[...byKind].map(([kind, facts]) => (
          <div key={kind} style={{ marginBottom: 12 }}>
            <div style={{ textTransform: 'uppercase', fontSize: 11, letterSpacing: 0.5, color: textMuted(isDark), marginBottom: 4 }}>{kind}</div>
            {facts.map(f => <FactRow key={f.id} fact={f} current={page.entity} onEntity={onEntity} onEdit={onEdit} onRetire={onRetire} />)}
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
      <div style={{ color: textSecondary(isDark) }}>
        Click a node to open its page. Larger nodes have more confirmed facts; dashed
        ones only have facts waiting in the inbox. Drag to pan, scroll to zoom.
      </div>
    )
  }
  return (
    <div style={{ width: 380, flexShrink: 0, overflow: 'auto', background: sf.raised, border: `1px solid ${sf.border}`, borderRadius: RADIUS.md, padding: 14 }}>
      {body}
    </div>
  )
}

function EntityTag({ entity, onClick }: { entity: string; onClick?: (e: string) => void }) {
  const [type, name] = splitEntity(entity)
  return (
    <Tag
      color={withAlpha(typeColor(type), 0.18)}
      style={{ color: 'inherit', cursor: onClick ? 'pointer' : 'default', fontFamily: MONO_FONT, borderColor: withAlpha(typeColor(type), 0.6), marginInlineEnd: 0 }}
      onClick={() => onClick?.(entity)}
    >
      {type}:{name}
    </Tag>
  )
}

function FactRow({ fact, current, onEntity, onEdit, onRetire, actions }: {
  fact: KnowledgeFact
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
        {fact.about.filter(a => a !== current).map(a => <EntityTag key={a} entity={a} onClick={onEntity} />)}
      </Space>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
        <Text style={{ fontSize: 12, color: textMuted(isDark) }}>
          ×{fact.confirmations} · {fact.last_confirmed}{fact.source ? ` · ${fact.source}` : ''}{fact.author ? ` · ${fact.author}` : ''}
        </Text>
        <div style={{ flex: 1 }} />
        {actions}
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

function Inbox({ facts, onApprove, onReject, onEdit, onEntity }: {
  facts: KnowledgeFact[]
  onApprove: (f: KnowledgeFact) => void
  onReject: (f: KnowledgeFact) => void
  onEdit: (f: KnowledgeFact) => void
  onEntity: (e: string) => void
}) {
  const isDark = useIsDark()
  const sf = surfaces(isDark)
  if (facts.length === 0) {
    return <Empty style={{ marginTop: 60 }} description={<span style={{ color: textSecondary(isDark) }}>No facts waiting for review.</span>} />
  }
  return (
    <div style={{ overflow: 'auto', maxWidth: 900, background: sf.raised, border: `1px solid ${sf.border}`, borderRadius: RADIUS.md, padding: '4px 14px' }}>
      <div style={{ color: textSecondary(isDark), padding: '8px 0' }}>
        Proposed by the agent, by an MCP client such as Claude Code, or suggested from your chats. Nothing joins the wiki until you approve it.
      </div>
      {facts.map(f => (
        <FactRow key={f.id} fact={f} onEntity={onEntity} actions={
          <Space size={4}>
            <Button size="small" icon={<CheckOutlined />} onClick={() => onApprove(f)}>Approve</Button>
            <Button size="small" icon={<EditOutlined />} onClick={() => onEdit(f)}>Edit</Button>
            <Button size="small" danger icon={<CloseOutlined />} onClick={() => onReject(f)}>Reject</Button>
          </Space>
        } />
      ))}
    </div>
  )
}

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
        <Form.Item label="About" help="Entities as type:name, e.g. host:web-01, runnable:tail-log, file:/var/log/app.log, term:BAU">
          <Select mode="tags" value={about} onChange={setAbout} tokenSeparators={[',']}
            options={suggestions.map(s => ({ value: s, label: s }))} placeholder="host:web-01" />
        </Form.Item>
        <Form.Item label="Fact" validateStatus={error ? 'error' : undefined} help={error || 'One or two specific sentences. No secrets — facts are shared with the team.'}>
          <Input.TextArea rows={3} value={text} onChange={e => setText(e.target.value)} />
        </Form.Item>
      </Form>
    </Modal>
  )
}

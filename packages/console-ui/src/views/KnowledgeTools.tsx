// The Knowledge tab's Tools view: the team tools wiki, read from the team's
// wiki repo (OKF Markdown pages that Memory Sync's Pull caches locally).
//
// Left: every installed runnable, grouped by venv, with whether the team has a
// page for it, plus the team's other pages (services, terms …). Centre: the
// selected runnable — a header from its own runspec.toml (description, verbs,
// args, autonomy, where it's installed), the team page's sections, and the
// facts about it: team facts not yet on a page, and this console's own facts in
// a dashed panel that never leaves this machine. Phase 1 only reads pages;
// editing them happens in the repo.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Empty, Input, Popconfirm, Space, Spin, Tag, Tooltip, Typography, message, theme } from 'antd'
import { ApartmentOutlined, EditOutlined, ExportOutlined, HistoryOutlined, MessageOutlined, PlayCircleOutlined, PlusOutlined, ReloadOutlined, SearchOutlined, TeamOutlined } from '@ant-design/icons'

import { bridge, type KnowledgeFact, type Runnable, type WikiCheck, type WikiDraft, type WikiPage, type WikiPages } from '../bridge'
import { DiffView, PageEditor, ProposeBar } from './KnowledgeEdits'
import { WikiCheckPanel } from './KnowledgeCheck'
import { Markdown } from '../components/Markdown'
import {
  MONO_FONT, RADIUS, surfaces, textColor, textFaint, textMuted, textSecondary, useAccent, useIsDark, withAlpha,
} from '../ThemeContext'
import { AskFirst } from '../components/risk'

const { Text } = Typography

const LAST_TOOL_KEY = 'rs.knowledge.tool'

export interface ToolEntry {
  name: string
  description: string
  hosts: string[]
  venvs: string[]
  autonomy: string
  verbs: string[]
  args: string[]
  page: WikiPage | null
}

/** One entry per runnable name (a tool on several hosts or venvs is one tool),
 *  joined with its team page. Sorted by name. Pure, for tests. */
export function buildToolIndex(runnables: Runnable[], pages: WikiPage[]): ToolEntry[] {
  const byPage = new Map(pages.filter(p => p.entity.startsWith('runnable:')).map(p => [p.entity.slice(9), p]))
  const byName = new Map<string, ToolEntry>()
  for (const r of runnables) {
    const e = byName.get(r.name) ?? {
      name: r.name, description: r.description ?? '', hosts: [], venvs: [], autonomy: r.autonomy ?? '',
      verbs: Object.keys(r.commands ?? {}),
      args: (r.args ?? []).filter(a => a.type !== 'password').map(a => a.name),
      page: byPage.get(r.name) ?? null,
    }
    if (!e.hosts.includes(r.host)) e.hosts.push(r.host)
    if (r.group && !e.venvs.includes(r.group)) e.venvs.push(r.group)
    if (!e.description && r.description) e.description = r.description
    byName.set(r.name, e)
  }
  return [...byName.values()].sort((a, b) => a.name.localeCompare(b.name))
}

/** Runnable names a piece of text mentions, longest first, so a "Works with"
 *  bullet can link to the tools it names. A name that is a plain word (`deploy`)
 *  only counts when it is marked up as code, bold or a link, so prose like
 *  "after a deploy" doesn't link; a name like `tail-log` counts anywhere.
 *  Pure, for tests. */
export function mentionedTools(text: string, names: string[]): string[] {
  const hits: string[] = []
  const clean = (s: string) => ` ${s.toLowerCase().replace(/[`*[\]()]/g, ' ')} `
  const marked = [...text.matchAll(/`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(/g)]
    .map(m => m[1] ?? m[2] ?? m[3] ?? '').join(' | ')
  let rest = clean(text)
  let inMarked = clean(marked)
  for (const n of [...names].sort((a, b) => b.length - a.length)) {
    const esc = n.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const re = new RegExp(`[^a-z0-9_-]${esc}[^a-z0-9_-]`)
    const plainWord = /^[a-z]+$/i.test(n)
    if (re.test(plainWord ? inMarked : rest)) {
      hits.push(n)
      rest = rest.replace(re, ' ')
      inMarked = inMarked.replace(re, ' ')
    }
  }
  return hits
}

function pageText(p: WikiPage): string {
  return [p.title, p.description, ...p.sections.map(s => `${s.heading} ${s.text}`)].join(' ').toLowerCase()
}

export function KnowledgeTools({ runnables, active, onOpenSettings, focus, onRunTool, onAskAbout, onRecentRuns, onShowConnections }: {
  runnables: Runnable[]
  active: boolean
  onOpenSettings?: (tab: string) => void
  // From a tool's page to using it: open its run form, ask the assistant
  // (prefills the composer; absent when AI is off), or its History.
  onRunTool?: (name: string) => void
  onAskAbout?: (name: string) => void
  onRecentRuns?: (name: string) => void
  // Open the fact graph centred on this page's entity.
  onShowConnections?: (entity: string) => void
  // Open this page or tool (a request from another view; `n` changes per request).
  focus?: { target: string; n: number } | null
}) {
  const isDark = useIsDark()
  const accent = useAccent()
  const { token } = theme.useToken()
  const sf = surfaces(isDark)
  const [wiki, setWiki] = useState<WikiPages | null>(null)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<'all' | 'nopage'>('all')
  const [selected, setSelected] = useState<string>(() => {
    try { return localStorage.getItem(LAST_TOOL_KEY) ?? '' } catch { return '' }
  })
  const [facts, setFacts] = useState<KnowledgeFact[] | null>(null)
  const [drafts, setDrafts] = useState<WikiDraft[]>([])
  const [drafted, setDrafted] = useState<Set<string>>(new Set())
  const [editing, setEditing] = useState<{ path: string; entity: string; description?: string } | null>(null)
  const [check, setCheck] = useState<WikiCheck | null>(null)
  const [proposedIn, setProposedIn] = useState<{ branch: string; url: string }>({ branch: '', url: '' })

  const load = useCallback(() => {
    bridge.knowledge_wiki_pages().then(setWiki).catch(() => setWiki({ pages: [], repo_url: '', ref: '', configured: false }))
    bridge.knowledge_drafts().then(r => {
      setDrafts(r?.drafts ?? []); setDrafted(new Set(r?.drafted ?? []))
      setProposedIn({ branch: r?.proposed_branch ?? '', url: r?.merge_request_url ?? '' })
    }).catch(() => { setDrafts([]) })
    bridge.knowledge_wiki_check().then(r => setCheck(r?.summary ? r : null)).catch(() => setCheck(null))
  }, [])
  useEffect(() => { if (active) load() }, [active, load])
  useEffect(() => {
    window.addEventListener('runspec:knowledge_updated', load)
    return () => window.removeEventListener('runspec:knowledge_updated', load)
  }, [load])

  const pages = useMemo(() => wiki?.pages ?? [], [wiki])
  const tools = useMemo(() => buildToolIndex(runnables, pages), [runnables, pages])
  const otherPages = useMemo(() => pages.filter(p => !p.entity.startsWith('runnable:')), [pages])
  const names = useMemo(() => tools.map(t => t.name), [tools])

  // Open a page or tool by entity or name. A runnable that isn't installed here
  // keeps its entity (`runnable:tail-log`): only installed tools are selected by name.
  const open = useCallback((target: string) => {
    setQuery(''); setFilter('all')
    const name = target.startsWith('runnable:') ? target.slice(9) : target
    setSelected(target.includes(':') && !names.includes(name) ? target : name)
  }, [names])
  useEffect(() => { if (focus?.target) open(focus.target) }, [focus?.n])  // eslint-disable-line react-hooks/exhaustive-deps
  // Team pages for runnables this console hasn't discovered (not installed here).
  const notHere = useMemo(() => {
    const have = new Set(names)
    return pages.filter(p => p.entity.startsWith('runnable:') && !have.has(p.entity.slice(9)))
  }, [pages, names])

  // Default: the last tool viewed, else the first with a page, else the first.
  useEffect(() => {
    if (!tools.length || wiki === null) return  // wait for the pages before choosing
    if (selected && (tools.some(t => t.name === selected) || pages.some(p => p.entity === selected))) return
    setSelected((tools.find(t => t.page) ?? tools[0]).name)
  }, [tools, pages, selected, wiki])
  useEffect(() => {
    try { if (selected) localStorage.setItem(LAST_TOOL_KEY, selected) } catch { /* private window */ }
  }, [selected])
  // Keep the selected row in sight, e.g. after a jump from the wiki check or the graph.
  const navRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const row = navRef.current?.querySelector('[aria-current="true"]') as HTMLElement | null
    row?.scrollIntoView?.({ block: 'nearest' })
  }, [selected, wiki])

  const isPage = selected.includes(':')
  const tool = isPage ? null : tools.find(t => t.name === selected) ?? null
  const page = isPage ? pages.find(p => p.entity === selected) ?? null : tool?.page ?? null
  const entity = isPage ? selected : `runnable:${selected}`
  const pagePath = page?.path ?? (entity.startsWith('runnable:') ? `runnables/${selected}.md` : '')
  const draft = drafts.find(d => d.path === pagePath) ?? null
  const notInstalled = isPage && selected.startsWith('runnable:')
  const actionName = tool?.name ?? (notInstalled ? selected.slice(9) : '')

  const addToPage = async (f: KnowledgeFact) => {
    try {
      const r = await bridge.knowledge_draft_fact(f.id)
      if (r.ok) message.success(`Added to ${r.path} under \u201c${r.heading}\u201d. Propose it when you are ready.`)
      else message.error(r.error || 'Could not add it to a page.')
    } catch (e) { message.error(String(e)) }
    load()
  }
  const convertAll = async () => {
    try {
      const r = await bridge.knowledge_convert_facts()
      message.success(`${r.facts} fact${r.facts === 1 ? '' : 's'} added to ${r.pages} page${r.pages === 1 ? '' : 's'}.${r.kept_local ? ` ${r.kept_local} stay on this console.` : ''}${r.no_page ? ` ${r.no_page} have no page to go on.` : ''}`)
    } catch (e) { message.error(String(e)) }
    load()
  }

  useEffect(() => {
    if (!selected) { setFacts(null); return }
    let live = true
    setFacts(null)
    bridge.knowledge_page(entity).then(p => { if (live) setFacts(p.facts) }).catch(() => { if (live) setFacts([]) })
    return () => { live = false }
  }, [entity, selected])

  const q = query.trim().toLowerCase()
  const shownTools = tools.filter(t => (filter === 'all' || !t.page) && (!q || t.name.toLowerCase().includes(q)))
  const pageHits = q ? pages.filter(p => pageText(p).includes(q) && !(p.entity.startsWith('runnable:') && p.title.toLowerCase().includes(q))).slice(0, 8) : []
  const byVenv = new Map<string, ToolEntry[]>()
  for (const t of shownTools) {
    const v = t.venvs[0] ?? 'other'
    byVenv.set(v, [...(byVenv.get(v) ?? []), t])
  }
  const noPage = tools.filter(t => !t.page).length

  const rowStyle = (on: boolean): React.CSSProperties => ({
    display: 'flex', alignItems: 'center', gap: 8, width: '100%', padding: '5px 8px', border: 0,
    borderRadius: RADIUS.sm, cursor: 'pointer', textAlign: 'left', font: 'inherit',
    background: on ? withAlpha(accent, 0.15) : 'transparent', color: on ? textColor(isDark) : textSecondary(isDark),
  })

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, flex: 1, minHeight: 0 }}>
    <ProposeBar drafts={drafts} configured={!!wiki?.configured} onChanged={load} onOpenSettings={onOpenSettings}
      proposedBranch={proposedIn.branch} mergeRequestUrl={proposedIn.url} />
    {pages.length > 0 && <WikiCheckPanel check={check} onOpen={open} />}
    <div data-testid="knowledge-tools" style={{ display: 'flex', flex: 1, minHeight: 0, border: `1px solid ${sf.border}`, borderRadius: RADIUS.md, overflow: 'hidden' }}>
      {/* index */}
      <nav aria-label="Tools" style={{ width: 250, flexShrink: 0, background: sf.sunken, borderRight: `1px solid ${sf.border}`, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        <div style={{ padding: 10, display: 'flex', flexDirection: 'column', gap: 8, borderBottom: `1px solid ${sf.border}` }}>
          <Input size="small" allowClear prefix={<SearchOutlined style={{ color: textMuted(isDark) }} />}
            placeholder="Find a tool or page" aria-label="Find a tool or page" value={query} onChange={e => setQuery(e.target.value)} />
          <Space size={4} wrap>
            <Tag.CheckableTag checked={filter === 'all'} onChange={() => setFilter('all')}>All tools {tools.length}</Tag.CheckableTag>
            {/* Only a filter when it narrows the list (some tools have a page, some don't). */}
            {noPage > 0 && noPage < tools.length && (
              <Tag.CheckableTag checked={filter === 'nopage'} onChange={() => setFilter('nopage')}>No page yet {noPage}</Tag.CheckableTag>
            )}
          </Space>
        </div>
        <div ref={navRef} style={{ overflowY: 'auto', padding: 6, flex: 1 }}>
          {pageHits.length > 0 && (
            <div style={{ marginBottom: 6 }}>
              <div style={groupStyle(isDark)}>Pages mentioning “{query.trim()}”</div>
              {pageHits.map(p => (
                <button key={p.path} type="button" style={rowStyle(selected === p.entity || selected === p.entity.slice(9))}
                  onClick={() => setSelected(p.entity.startsWith('runnable:') ? p.entity.slice(9) : p.entity)}>
                  <span style={{ fontFamily: MONO_FONT, fontSize: 13 }}>{p.title}</span>
                  <span style={{ marginLeft: 'auto', fontSize: 11, color: textMuted(isDark) }}>{p.entity.split(':')[0]}</span>
                </button>
              ))}
            </div>
          )}
          {[...byVenv.entries()].map(([venv, list]) => (
            <div key={venv}>
              <div style={groupStyle(isDark)}><span>{venv}</span><span style={{ fontFamily: MONO_FONT, fontWeight: 500, letterSpacing: 0 }}>{list.length}</span></div>
              {list.map(t => (
                (() => {
                  const edited = drafts.some(d => d.path === `runnables/${t.name}.md`)
                  // One name for the row, so a screen reader hears "backup, has a team page, edited on this console".
                  const label = [t.name, t.page ? 'has a team page' : 'no page yet', ...(edited ? ['edited on this console'] : [])].join(', ')
                  return (
                    <button key={t.name} type="button" aria-current={selected === t.name ? 'true' : undefined} aria-label={label}
                      data-testid={`tool-${t.name}`} style={rowStyle(selected === t.name)} onClick={() => setSelected(t.name)}>
                      <span style={{ fontFamily: MONO_FONT, fontSize: 13 }}>{t.name}</span>
                      <span aria-hidden="true" style={{ marginLeft: 'auto', fontSize: 11, display: 'flex', gap: 6, alignItems: 'center' }}>
                        {edited && <Tooltip title="Edited on this console"><EditOutlined style={{ color: accent }} /></Tooltip>}
                        {t.page && <Tooltip title="Has a team page"><span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 4, background: token.colorSuccess }} /></Tooltip>}
                      </span>
                    </button>
                  )
                })()
              ))}
            </div>
          ))}
          {!q && filter === 'all' && notHere.length > 0 && (
            <div>
              <div style={groupStyle(isDark)}><span>Not installed here</span><span style={{ fontFamily: MONO_FONT, fontWeight: 500, letterSpacing: 0 }}>{notHere.length}</span></div>
              {notHere.map(p => (
                <button key={p.path} type="button" data-testid={`page-${p.entity}`} aria-current={selected === p.entity ? 'true' : undefined}
                  style={rowStyle(selected === p.entity)} onClick={() => setSelected(p.entity)}>
                  <span style={{ fontFamily: MONO_FONT, fontSize: 13, color: selected === p.entity ? textColor(isDark) : textMuted(isDark) }}>{p.title}</span>
                </button>
              ))}
            </div>
          )}
          {!q && filter === 'all' && otherPages.length > 0 && (
            <div>
              <div style={groupStyle(isDark)}><span>Other pages</span><span style={{ fontFamily: MONO_FONT, fontWeight: 500, letterSpacing: 0 }}>{otherPages.length}</span></div>
              {otherPages.map(p => (
                <button key={p.path} type="button" aria-current={selected === p.entity ? 'true' : undefined}
                  style={rowStyle(selected === p.entity)} onClick={() => setSelected(p.entity)}>
                  <span style={{ fontSize: 13 }}>{p.title}</span>
                  <span style={{ marginLeft: 'auto', fontSize: 11, color: textMuted(isDark) }}>{p.entity.split(':')[0]}</span>
                </button>
              ))}
            </div>
          )}
          {tools.length === 0 && (
            <Text style={{ display: 'block', padding: 10, fontSize: 12, color: textMuted(isDark) }}>
              No runnables discovered yet. They appear here once the console has found the tools on your hosts.
            </Text>
          )}
        </div>
      </nav>

      {/* page */}
      <article aria-label={tool ? `${tool.name} page` : page?.title ?? 'Page'} style={{ flex: 1, minWidth: 0, overflowY: 'auto', padding: '18px 26px', display: 'flex', flexDirection: 'column', gap: 14 }}>
        {wiki === null ? (
          <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}><Spin /></div>
        ) : !tool && !page ? (
          <Empty description={<span style={{ color: textSecondary(isDark) }}>Pick a tool on the left.</span>} style={{ marginTop: 60 }} />
        ) : (
          <>
            {page && (
              <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, fontSize: 12, color: textMuted(isDark) }}>
                <span style={{ fontFamily: MONO_FONT, color: textSecondary(isDark) }}>{page.path}</span>
                {notInstalled && (
                  <Tooltip title="No connected host has this runnable, so it can't be run from here.">
                    <Tag bordered={false} style={{ margin: 0 }}>not installed here</Tag>
                  </Tooltip>
                )}
                {page.status && (
                  <Tag bordered={false} color={page.status === 'stable' ? 'green' : page.status === 'deprecated' ? 'red' : 'default'}
                    style={{ margin: 0, ...(isDark ? {} : { color: page.status === 'stable' ? '#1a7f37' : page.status === 'deprecated' ? '#c4232d' : undefined }) }}>
                    {page.status}
                  </Tag>
                )}
                {page.verified_by && <span>verified by {page.verified_by.replace(/^human:/, '')}</span>}
                {page.stale_after && page.stale_after < new Date().toISOString().slice(0, 10) && (
                  <Tag bordered={false} color="gold" style={{ margin: 0 }}>check again: stale since {page.stale_after}</Tag>
                )}
                <span style={{ marginLeft: 'auto', display: 'flex', gap: 12 }}>
                  <a role="button" onClick={() => setEditing({ path: page.path, entity: page.entity })} style={{ color: accent }}>
                    <EditOutlined /> Edit page
                  </a>
                  {page.url && (
                    <a href={page.url} target="_blank" rel="noreferrer" style={{ color: accent }}>
                      <ExportOutlined /> Open in repo
                    </a>
                  )}
                </span>
              </div>
            )}
            <div>
              <h2 style={{ margin: 0, fontFamily: tool ? MONO_FONT : undefined, fontSize: 20, fontWeight: 650, color: textColor(isDark) }}>
                {tool ? tool.name : page?.title}
              </h2>
              {(tool?.description || page?.description) && (
                <p style={{ margin: '4px 0 0', color: textSecondary(isDark), maxWidth: '72ch' }}>{tool?.description || page?.description}</p>
              )}
              {tool && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 14px', marginTop: 8, fontSize: 12, color: textMuted(isDark) }}>
                  <span title="From the runnable's runspec.toml">From runspec.toml</span>
                  <span>on {tool.hosts.length} host{tool.hosts.length === 1 ? '' : 's'}{tool.hosts.length <= 3 ? `: ${tool.hosts.join(', ')}` : ''}</span>
                  {tool.autonomy && <span>{tool.autonomy}</span>}
                  {tool.verbs.length > 0 && <span>verbs: <span style={{ fontFamily: MONO_FONT }}>{tool.verbs.slice(0, 8).join(' ')}{tool.verbs.length > 8 ? ' …' : ''}</span></span>}
                  {tool.args.length > 0 && <span>args: <span style={{ fontFamily: MONO_FONT }}>{tool.args.slice(0, 8).join(' ')}{tool.args.length > 8 ? ' …' : ''}</span></span>}
                </div>
              )}
              {(actionName || onShowConnections) && (onRunTool || onAskAbout || onRecentRuns || onShowConnections) && (
                <div data-testid="tool-actions" style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10 }}>
                  {tool && onRunTool && <Button size="small" icon={<PlayCircleOutlined />} onClick={() => onRunTool(tool.name)}>Run…</Button>}
                  {actionName && onAskAbout && <Button size="small" icon={<MessageOutlined />} onClick={() => onAskAbout(actionName)}>Ask about {actionName}</Button>}
                  {actionName && onRecentRuns && <Button size="small" icon={<HistoryOutlined />} onClick={() => onRecentRuns(actionName)}>Recent runs</Button>}
                  {onShowConnections && <Button size="small" type="text" icon={<ApartmentOutlined />} onClick={() => onShowConnections(entity)}>Show connections</Button>}
                </div>
              )}
            </div>

            {draft && (
              <div data-testid="page-draft" style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <Text strong style={{ color: textColor(isDark) }}>Your edit to this page</Text>
                  <Text style={{ fontSize: 12, color: textMuted(isDark) }}>
                    {draft.proposed
                      ? <>proposed in {proposedIn.branch || 'your contrib branch'}, waiting for merge{proposedIn.url && <> · <a href={proposedIn.url} target="_blank" rel="noreferrer">Open merge request</a></>}</>
                      : 'not proposed yet'} · +{draft.added} −{draft.removed}
                  </Text>
                  <span style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
                    <Button size="small" icon={<EditOutlined />} onClick={() => setEditing({ path: draft.path, entity: draft.entity })}>Edit</Button>
                    <AskFirst onConfirm={() => { bridge.knowledge_draft_discard(draft.path).then(load).catch(load) }} ask={{
                      level: 'local', title: 'Discard your edit to this page?', action: draft.path,
                      consequence: draft.proposed ? 'It stays in your merge request until you change that branch.' : 'It can\u2019t be undone.',
                      okText: 'Discard',
                    }}>
                      <Button size="small">Discard</Button>
                    </AskFirst>
                  </span>
                </div>
                <DiffView diff={draft.diff} />
              </div>
            )}

            {page?.error && <Alert type="warning" showIcon message={`${page.path}: ${page.error}. It is shown as written.`} />}

            {page ? page.sections.map((s, i) => {
              const linked = /^works with$/i.test(s.heading) ? mentionedTools(s.text, names).filter(n => n !== tool?.name) : []
              return (
                <section key={i}>
                  {s.heading && <h3 style={headingStyle(isDark)}>{s.heading}</h3>}
                  <div style={{ maxWidth: '76ch' }}><Markdown color={textColor(isDark)} fontSize={14}>{s.text}</Markdown></div>
                  {linked.length > 0 && (
                    <Space size={4} wrap style={{ marginTop: 4 }}>
                      {linked.map(n => (
                        <Tag key={n} bordered={false} style={{ cursor: 'pointer', fontFamily: MONO_FONT, color: accent, background: withAlpha(accent, 0.12) }}
                          onClick={() => setSelected(n)}>{n} →</Tag>
                      ))}
                    </Space>
                  )}
                </section>
              )
            }) : (
              <div data-testid="no-page" style={{ padding: '14px 16px', borderRadius: RADIUS.md, background: sf.raised, border: `1px solid ${sf.border}` }}>
                <Text style={{ color: textColor(isDark) }}>No team page for <span style={{ fontFamily: MONO_FONT }}>{tool?.name}</span> yet.</Text>
                <div style={{ marginTop: 4, color: textSecondary(isDark), fontSize: 13 }}>
                  {wiki.configured
                    ? <>Start one here: it opens with “What it's for”, “Works with” and “Watch out for”, and you propose it to the team when it's ready.</>
                    : <>Start one here, and set up a team wiki repo to share it with your team.</>}
                </div>
                <Space size={8} style={{ marginTop: 8 }}>
                  {tool && !draft && (
                    <Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => setEditing({ path: `runnables/${tool.name}.md`, entity: `runnable:${tool.name}`, description: tool.description })}>Start a page</Button>
                  )}
                  {!wiki.configured && (
                    <Button size="small" icon={<TeamOutlined />} onClick={() => onOpenSettings?.('memorySync')}>Share with team</Button>
                  )}
                </Space>
                {wiki.configured && pages.length === 0 && (
                  <Button size="small" icon={<ReloadOutlined />} style={{ marginTop: 8 }} onClick={() => bridge.memory_sync_pull().then(load).catch(load)}>Pull the team wiki</Button>
                )}
              </div>
            )}

            <ToolFacts facts={facts} drafted={drafted} onAddToPage={addToPage} onConvertAll={convertAll} />
          </>
        )}
      </article>
    </div>
    {editing && <PageEditor path={editing.path} entity={editing.entity} description={editing.description} onClose={() => setEditing(null)} onSaved={load} />}
    </div>
  )
}

function ToolFacts({ facts, drafted, onAddToPage, onConvertAll }: {
  facts: KnowledgeFact[] | null
  drafted: Set<string>
  onAddToPage: (f: KnowledgeFact) => void
  onConvertAll: () => void
}) {
  const isDark = useIsDark()
  if (facts === null) return null
  const live = facts.filter(f => f.status !== 'retired')
  const team = live.filter(f => f.scope !== 'local' && f.status === 'approved' && f.source !== 'seed' && !drafted.has(f.id))
  const local = live.filter(f => f.scope === 'local')
  return (
    <>
      {team.length > 0 && (
        <section>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
            <h3 style={headingStyle(isDark)}>Team facts not on the page yet</h3>
            <AskFirst onConfirm={onConvertAll} ask={{
              level: 'local', danger: false, title: 'Add every approved team fact to its page?',
              action: `${team.length} team fact${team.length === 1 ? '' : 's'} become page edits`,
              consequence: 'You see each edit before you propose it, and can discard it.', okText: 'Add to pages',
            }}>
              <Button size="small" type="link" style={{ padding: 0, height: 'auto', fontSize: 12 }}>Add all facts to pages</Button>
            </AskFirst>
          </div>
          <ul style={{ margin: 0, paddingLeft: 18, display: 'grid', gap: 6, color: textColor(isDark), maxWidth: '76ch' }}>
            {team.map(f => (
              <li key={f.id}>
                {f.text}{' '}
                <Button size="small" type="link" icon={<PlusOutlined />} onClick={() => onAddToPage(f)} style={{ padding: 0, height: 'auto' }}>Add to page</Button>
              </li>
            ))}
          </ul>
        </section>
      )}
      {local.length > 0 && (
        <details data-testid="this-console-facts" style={{ border: `1px dashed ${surfaces(isDark).borderStrong}`, borderRadius: RADIUS.md, padding: '8px 12px', color: textSecondary(isDark) }}>
          <summary style={{ cursor: 'pointer' }}>
            <Tooltip title="Facts tied to this console's own hosts, schedules or history. They help the assistant here and are never shared.">
              <span>This console · {local.length} fact{local.length === 1 ? '' : 's'}, never shared</span>
            </Tooltip>
          </summary>
          <ul style={{ margin: '6px 0 0', paddingLeft: 18, display: 'grid', gap: 4 }}>
            {local.map(f => <li key={f.id}>{f.text}</li>)}
          </ul>
        </details>
      )}
    </>
  )
}

function groupStyle(isDark: boolean): React.CSSProperties {
  return {
    display: 'flex', justifyContent: 'space-between', padding: '10px 8px 4px', fontSize: 11, fontWeight: 600,
    letterSpacing: '0.06em', textTransform: 'uppercase', color: textSecondary(isDark),
  }
}

function headingStyle(isDark: boolean): React.CSSProperties {
  return { margin: '0 0 6px', fontSize: 12, fontWeight: 600, letterSpacing: '0.06em', textTransform: 'uppercase', color: textMuted(isDark) }
}

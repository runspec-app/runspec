import { useEffect, useMemo, useRef, useState } from 'react'
import { Table, Tag, Switch, Alert, Button, Tooltip, Empty, Space, Collapse, Input, Drawer, message } from 'antd'
import { CheckOutlined, CloseOutlined, ReloadOutlined, BellOutlined, SendOutlined, StopOutlined, CopyOutlined } from '@ant-design/icons'
import { bridge, type TriggerCard, type TriggerEvent, type TriggerListResult } from '../bridge'
import { useIsDark, textColor } from '../ThemeContext'
import { Markdown } from '../components/Markdown'

// A status counts as "in flight" (abortable) while the engine is still working
// it — anything before a terminal done/failed. The Stop button calls
// bridge.cancel_chat(card_id): a trigger run's chat_id IS its card_id, so this
// trips the same cancel event + unblocks a parked Approve/Deny gate.
const RUNNING_STATUSES = new Set(['triggered', 'triage', 'running'])

// Right-click copy is suppressed outside webview debug mode, so offer an
// explicit Copy control. Selectable text already supports Ctrl+C.
function CopyButton({ text, title = 'Copy' }: { text: string; title?: string }) {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      message.success('Copied')
    } catch {
      message.error('Copy failed')
    }
  }
  return (
    <Tooltip title={title}>
      <Button size="small" type="text" icon={<CopyOutlined />} onClick={e => { e.stopPropagation(); copy() }} />
    </Tooltip>
  )
}

// A chat card is a helpdesk-room thread surfaced in the Triggers inbox (source
// === 'chat'); clicking it opens the live conversation pane. A portal card
// (source === 'portal') is a runnables-portal request the hub routed to this
// console — it opens the detail drawer, where the autonomy-gate Approve/Deny and
// the enactment result render (the claim-free request lifecycle, no claim
// controls). See docs/design/runnables-portal.md.
type QueueMsg = { sender?: string; text?: string; id?: string; ts?: number; kind?: string }
const isChatCard = (c: TriggerCard) => c.source === 'chat'
const visitorOf = (room: string) => room.split('/').slice(2).join('/') || room

// Run lifecycle statuses, in order. The engine drives triggered→…→done/failed; the
// awaiting_human column is overlaid client-side from a card-scoped tool_confirm.
const COLUMNS: { key: string; label: string }[] = [
  { key: 'triggered', label: 'Triggered' },
  { key: 'triage', label: 'Triage' },
  { key: 'awaiting_human', label: 'Awaiting human' },
  { key: 'running', label: 'Running' },
  { key: 'done', label: 'Done' },
  { key: 'failed', label: 'Failed' },
]

const COLUMN_COLOR: Record<string, string> = {
  triggered: '#8c8c8c',
  triage: '#1677ff',
  awaiting_human: '#fa8c16',
  running: '#13c2c2',
  done: '#52c41a',
  failed: '#ff4d4f',
}

const STATUS_LABEL: Record<string, string> = Object.fromEntries(COLUMNS.map(c => [c.key, c.label]))

function StatusBadge({ status }: { status: string }) {
  const color = COLUMN_COLOR[status] ?? '#8c8c8c'
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color, flexShrink: 0, width: 110 }}>
      <span style={{ width: 8, height: 8, borderRadius: 8, background: color }} />
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

interface PendingConfirm {
  request_id: string
  tool_name: string
  tool_input: Record<string, unknown>
  // Human-readable resolved view (unwrapped runnable + args for search-mode
  // run_runnable); equals tool_name/_input for plain calls. Optional for back-compat.
  display_name?: string
  display_input?: Record<string, unknown>
}

// Render a tool's input for the approve prompt — the draft body for draft_reply,
// else compact key=value lines for a runnable's args.
function describeToolInput(toolName: string, input: Record<string, unknown>): string {
  if (toolName === 'draft_reply' && typeof input.body === 'string') return input.body
  const entries = Object.entries(input ?? {})
  if (entries.length === 0) return ''
  return entries.map(([k, v]) => `${k} = ${typeof v === 'string' ? v : JSON.stringify(v)}`).join('\n')
}

function fmtTime(epochSeconds: number): string {
  if (!epochSeconds) return ''
  return new Date(epochSeconds * 1000).toLocaleTimeString()
}

export function TriggersView() {
  const isDark = useIsDark()
  const [cards, setCards] = useState<Record<string, TriggerCard>>({})
  const [pending, setPending] = useState<Record<string, PendingConfirm>>({})
  const [config, setConfig] = useState<TriggerListResult | null>(null)
  // Streamed agent text per card id (its reasoning/reply as it arrives).
  const [streams, setStreams] = useState<Record<string, string>>({})
  // The run whose detail drawer is open (card_id), or null.
  const [selected, setSelected] = useState<string | null>(null)
  // The queue conversation shown in the bottom pane (card_id), or null.
  const [convo, setConvo] = useState<string | null>(null)
  // Live transcript per conversation room, and the composer draft per room.
  const [messages, setMessages] = useState<Record<string, QueueMsg[]>>({})
  const [convoDraft, setConvoDraft] = useState<Record<string, string>>({})
  const [sending, setSending] = useState(false)
  // Event-audit feed: every event seen + its disposition (newest first).
  const [events, setEvents] = useState<TriggerEvent[]>([])
  // Draft replies an agent-mode trigger saved, keyed by card id — drives the
  // card's Send button (review the draft, then send it from here).
  const [drafts, setDrafts] = useState<
    Record<string, { entry_id: string; to: string; subject: string; sent?: boolean; sending?: boolean }>
  >({})
  // Known card ids, so the global runspec:token listener ignores chat tokens.
  const cardIds = useRef<Set<string>>(new Set())

  const loadConfig = () => bridge.list_triggers().then(setConfig).catch(console.error)

  useEffect(() => {
    loadConfig()
    bridge.get_trigger_board().then(board => {
      board.forEach(c => cardIds.current.add(c.card_id))
      setCards(Object.fromEntries(board.map(c => [c.card_id, c])))
    }).catch(console.error)
    bridge.get_trigger_events().then(evs => setEvents([...evs].reverse())).catch(console.error)
  }, [])

  // Live updates from the bridge.
  useEffect(() => {
    const onCard = (e: Event) => {
      const card = (e as CustomEvent).detail as TriggerCard
      cardIds.current.add(card.card_id)
      setCards(prev => ({ ...prev, [card.card_id]: card }))
    }
    const onToken = (e: Event) => {
      const { id, token } = (e as CustomEvent).detail as { id: string; token: string }
      if (!cardIds.current.has(id)) return  // ignore main-chat tokens
      setStreams(s => ({ ...s, [id]: (s[id] ?? '') + token }))
    }
    const onConfirm = (e: Event) => {
      const { id, request_id, tool_name, tool_input, display_name, display_input } = (e as CustomEvent).detail as {
        id: string; request_id: string; tool_name: string; tool_input: Record<string, unknown>
        display_name?: string; display_input?: Record<string, unknown>
      }
      // Only trigger-card confirms (chat confirms use chat ids we don't track).
      setCards(prev => {
        if (prev[id]) {
          setPending(p => ({ ...p, [id]: { request_id, tool_name, tool_input: tool_input ?? {}, display_name, display_input } }))
        }
        return prev
      })
    }
    const onEnd = (e: Event) => {
      const { id } = (e as CustomEvent).detail as { id: string }
      setPending(p => {
        if (!p[id]) return p
        const next = { ...p }; delete next[id]; return next
      })
    }
    const onStatus = () => loadConfig()
    const onMessage = (e: Event) => {
      const m = (e as CustomEvent).detail as QueueMsg & { room?: string }
      const room = m.room || ''
      if (!room.startsWith('helpdesk/')) return  // queue conversations only
      setMessages(prev => {
        const list = prev[room] || []
        if (m.id && list.some(x => x.id === m.id)) return prev  // dedupe by id
        return { ...prev, [room]: [...list, { sender: m.sender, text: m.text, id: m.id, ts: m.ts, kind: m.kind }] }
      })
    }
    const onRoomDraft = (e: Event) => {
      // An agent-proposed reply pre-fills the conversation composer (the human
      // reviews + sends) and brings that conversation into view.
      const d = (e as CustomEvent).detail as { room?: string; text?: string }
      const room = d.room || ''
      if (!room.startsWith('helpdesk/')) return
      setConvoDraft(prev => ({ ...prev, [room]: d.text || '' }))
      setConvo(`queue:${room}`)
    }
    const onEvent = (e: Event) => {
      const rec = (e as CustomEvent).detail as TriggerEvent
      setEvents(prev => [rec, ...prev].slice(0, 300))
    }
    const onDraft = (e: Event) => {
      const { id, entry_id, to, subject } = (e as CustomEvent).detail as {
        id: string; entry_id: string; to: string; subject: string
      }
      if (entry_id) setDrafts(prev => ({ ...prev, [id]: { entry_id, to, subject } }))
    }
    window.addEventListener('runspec:trigger_card', onCard)
    window.addEventListener('runspec:token', onToken)
    window.addEventListener('runspec:tool_confirm', onConfirm)
    window.addEventListener('runspec:run_end', onEnd)
    window.addEventListener('runspec:trigger_status', onStatus)
    window.addEventListener('runspec:room_message', onMessage)
    window.addEventListener('runspec:room_draft', onRoomDraft)
    window.addEventListener('runspec:trigger_event', onEvent)
    window.addEventListener('runspec:trigger_draft', onDraft)
    return () => {
      window.removeEventListener('runspec:trigger_card', onCard)
      window.removeEventListener('runspec:token', onToken)
      window.removeEventListener('runspec:tool_confirm', onConfirm)
      window.removeEventListener('runspec:run_end', onEnd)
      window.removeEventListener('runspec:trigger_status', onStatus)
      window.removeEventListener('runspec:room_message', onMessage)
      window.removeEventListener('runspec:room_draft', onRoomDraft)
      window.removeEventListener('runspec:trigger_event', onEvent)
      window.removeEventListener('runspec:trigger_draft', onDraft)
    }
  }, [])

  const sendDraft = async (cardId: string) => {
    const d = drafts[cardId]
    if (!d || d.sending || d.sent) return
    setDrafts(prev => ({ ...prev, [cardId]: { ...prev[cardId], sending: true } }))
    try {
      const res = await bridge.send_trigger_draft(d.entry_id)
      setDrafts(prev => ({
        ...prev,
        [cardId]: { ...prev[cardId], sending: false, sent: !!res.ok },
      }))
    } catch (err) {
      console.error(err)
      setDrafts(prev => ({ ...prev, [cardId]: { ...prev[cardId], sending: false } }))
    }
  }

  const answer = (cardId: string, approved: boolean) => {
    const p = pending[cardId]
    if (!p) return
    bridge.resolve_tool_confirmation(p.request_id, approved).catch(console.error)
    setPending(prev => { const next = { ...prev }; delete next[cardId]; return next })
  }

  const sendReply = async (room: string) => {
    const text = (convoDraft[room] || '').trim()
    if (!text) return
    setSending(true)
    try {
      const res = await bridge.send_room_message(room, text)
      if (res.ok) setConvoDraft(prev => ({ ...prev, [room]: '' }))
    } catch (err) { console.error(err) }
    finally { setSending(false) }
  }

  const toggleEnabled = async (name: string, enabled: boolean) => {
    await bridge.set_trigger_enabled(name, enabled)
    loadConfig()
  }

  // Runs as an inbox list, newest first. Click a row → the detail drawer.
  const runs = useMemo(
    () => Object.values(cards).sort((a, b) => b.created_at - a.created_at),
    [cards],
  )
  const statusOf = (card: TriggerCard) =>
    pending[card.card_id] ? 'awaiting_human' : card.column
  const selectedCard = selected ? cards[selected] : undefined
  const convoCard = convo ? cards[convo] : undefined
  const convoRoom = (convoCard?.detail?.room as string | undefined) || ''

  const dim = isDark ? '#888' : '#999'
  const cardBg = isDark ? '#1f1f1f' : '#fafafa'
  const colBg = isDark ? '#141414' : '#f0f0f0'
  const border = isDark ? '#303030' : '#e8e8e8'

  const sources = config?.sources ?? []
  const triggers = config?.triggers ?? []
  const errors = config?.errors ?? []

  // Only warn about an unavailable source that a configured trigger actually
  // uses — no point nagging about, say, the notifications source on a machine
  // that only runs Outlook triggers.
  const usedSources = new Set(triggers.map(t => String(t.source)))

  const configColumns = [
    {
      title: 'Trigger',
      dataIndex: 'name',
      key: 'name',
      render: (n: string) => <strong>{n}</strong>,
    },
    {
      title: 'On',
      key: 'on',
      render: (_: unknown, t: Record<string, unknown>) => (
        <span style={{ fontFamily: 'monospace', fontSize: 12 }}>
          {String(t.source)}:{String(t.event)}
        </span>
      ),
    },
    {
      title: 'Mode',
      dataIndex: 'mode',
      key: 'mode',
      render: (m: string) => <Tag color={m === 'rule' ? 'geekblue' : 'purple'}>{m ?? 'agent'}</Tag>,
    },
    {
      title: 'Reply',
      key: 'reply',
      render: (_: unknown, t: Record<string, unknown>) => {
        const reply = t.reply as { draft?: boolean } | undefined
        return reply?.draft ? <Tag color="gold">draft</Tag> : <span style={{ color: dim }}>—</span>
      },
    },
    {
      title: 'Enabled',
      key: 'enabled',
      width: 90,
      render: (_: unknown, t: Record<string, unknown>) => (
        <Switch
          size="small"
          checked={t.enabled !== false}
          onChange={v => toggleEnabled(String(t.name), v)}
        />
      ),
    },
  ]

  return (
    <div style={{ color: textColor(isDark) }}>
      {/* Source availability (only for sources a configured trigger uses) */}
      {sources.map(s => (
        !s.available && usedSources.has(s.id) ? (
          <Alert
            key={s.id}
            type="warning"
            showIcon
            closable
            style={{ marginBottom: 8 }}
            message={<span>Event source <code>{s.id}</code> is unavailable: {s.reason}</span>}
          />
        ) : null
      ))}
      {errors.length > 0 && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message="Some triggers could not be loaded"
          description={<ul style={{ margin: 0, paddingLeft: 18 }}>{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>}
        />
      )}

      {/* Configured triggers — collapsible, like Working hours below */}
      <Collapse
        size="small"
        defaultActiveKey={['triggers']}
        style={{ marginBottom: 20 }}
        items={[{
          key: 'triggers',
          label: (
            <span>
              <BellOutlined /> Triggers
              <span style={{ color: dim, fontSize: 12 }}>
                {' · '}{triggers.length} configured{config?.running ? ' · engine running' : ''}
              </span>
            </span>
          ),
          extra: (
            <Tooltip title="Reload trigger config">
              <ReloadOutlined
                onClick={e => { e.stopPropagation(); loadConfig() }}
                style={{ color: dim }}
              />
            </Tooltip>
          ),
          children: triggers.length === 0 ? (
            <Empty
              description={
                <span style={{ color: dim }}>
                  No triggers configured. Add <code>[triggers.&lt;name&gt;]</code>{' '}
                  tables to <code>runspec_triggers.toml</code> in the app config
                  directory.
                </span>
              }
            />
          ) : (
            <Table
              dataSource={triggers}
              columns={configColumns}
              rowKey={(t: Record<string, unknown>) => String(t.name)}
              size="small"
              pagination={false}
            />
          ),
        }]}
      />

      {/* Runs — inbox list; click a row for the full detail drawer */}
      {runs.length === 0 ? (
        <Empty description={<span style={{ color: dim }}>No trigger runs yet.</span>} />
      ) : (
        <div style={{ border: `1px solid ${border}`, borderRadius: 6 }}>
          {runs.map((card, i) => {
            // A chat card opens the live conversation pane (it reads the room hub);
            // everything else (portal requests, matched triggers, reminders) opens
            // the detail drawer, where the Approve/Deny gate + result render.
            const chat = isChatCard(card)
            return (
            <div
              key={card.card_id}
              onClick={() => (chat ? setConvo(card.card_id) : setSelected(card.card_id))}
              style={{
                display: 'flex', gap: 8, alignItems: 'center', padding: '8px 10px',
                borderBottom: i < runs.length - 1 ? `1px solid ${border}` : undefined,
                cursor: 'pointer', fontSize: 12,
                background: selected === card.card_id || convo === card.card_id ? colBg : undefined,
              }}
            >
              <StatusBadge status={statusOf(card)} />
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 600 }}>
                {card.summary}
              </span>
              <span style={{ color: dim, flexShrink: 0 }}>{card.trigger}</span>
              {pending[card.card_id] && (
                <Button
                  size="small" type="primary"
                  onClick={e => { e.stopPropagation(); setSelected(card.card_id) }}
                >
                  Review
                </Button>
              )}
              <span style={{ color: dim, fontFamily: 'monospace', flexShrink: 0 }}>{fmtTime(card.created_at)}</span>
            </div>
            )
          })}
        </div>
      )}

      {/* Run detail — the large view: full event, agent output, timeline, gate */}
      <Drawer
        open={!!selectedCard}
        onClose={() => setSelected(null)}
        width="min(820px, 92vw)"
        title={selectedCard?.summary}
      >
        {selectedCard && (() => {
          const card = selectedCard
          const p = pending[card.card_id]
          const result = card.detail?.result as string | undefined
          const error = card.detail?.error as string | undefined
          const body = result || streams[card.card_id]
          // Prefer the human-readable resolved view (unwrapped runnable + args
          // for search-mode run_runnable); falls back to the raw tool for plain
          // calls. The draft_reply branch below still keys off the raw tool_name.
          const preview = p ? describeToolInput(p.display_name ?? p.tool_name, p.display_input ?? p.tool_input) : ''
          const draft = drafts[card.card_id]
          return (
            <div style={{ fontSize: 13, color: textColor(isDark) }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
                <StatusBadge status={statusOf(card)} />
                <span style={{ color: dim }}>{card.trigger} · {card.source}:{card.event_type}</span>
                {card.detail?.owner ? <span style={{ color: dim }}>· owner {String(card.detail.owner)}</span> : null}
                <span style={{ color: dim, fontFamily: 'monospace', marginLeft: 'auto' }}>{fmtTime(card.created_at)}</span>
                {/* Abort an in-flight run. The card's chat_id == card_id, so this
                    trips the run's cancel event (and any parked confirm gate). */}
                {RUNNING_STATUSES.has(card.column) && (
                  <Button
                    size="small" danger icon={<StopOutlined />}
                    onClick={() => {
                      bridge.cancel_chat(card.card_id).catch(console.error)
                      message.info('Stopping run…')
                    }}
                  >
                    Stop
                  </Button>
                )}
              </div>

              {/* A portal request the hub routed here: what the visitor asked for.
                  Approve/Deny below enacts it through the autonomy gate; the result
                  + timeline land in this same drawer as the audit record. */}
              {card.source === 'portal' ? (
                <div style={{ marginBottom: 16, border: `1px solid ${border}`, borderRadius: 6, padding: 12 }}>
                  <div style={{ color: dim, fontSize: 12, marginBottom: 4 }}>
                    {/* email-verify signup carries a display name + team; show
                        "Name <email>" when present, else just the verified id, with
                        the visitor's team as a tag. */}
                    {card.detail?.visitor_name
                      ? `${String(card.detail.visitor_name)} <${String(card.detail?.visitor ?? '')}>`
                      : String(card.detail?.visitor ?? 'A visitor')}
                    {card.detail?.team ? (
                      <Tag color="geekblue" style={{ margin: '0 0 0 6px', fontSize: 10 }}>{String(card.detail.team)}</Tag>
                    ) : null}{' '}
                    requested
                  </div>
                  <div style={{ fontFamily: 'monospace', marginBottom: 6 }}>{String(card.detail?.runnable ?? '')}</div>
                  {!!card.detail?.args && Object.keys(card.detail.args as object).length > 0 && (
                    <div style={{ whiteSpace: 'pre-wrap', fontSize: 12, color: dim }}>
                      {Object.entries(card.detail.args as Record<string, unknown>)
                        .map(([k, v]) => `${k} = ${typeof v === 'string' ? v : JSON.stringify(v)}`)
                        .join('\n')}
                    </div>
                  )}
                </div>
              ) : null}

              {p && (
                <div style={{
                  border: `1px solid ${COLUMN_COLOR.awaiting_human}`, borderRadius: 6,
                  padding: 12, marginBottom: 16,
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
                    <span style={{ color: COLUMN_COLOR.awaiting_human, fontWeight: 600 }}>
                      Approve <code>{p.display_name ?? p.tool_name}</code>?
                    </span>
                    {preview && <CopyButton text={preview} title="Copy" />}
                  </div>
                  {preview && (
                    <div style={{
                      maxHeight: 240, overflowY: 'auto',
                      background: colBg, border: `1px solid ${border}`, borderRadius: 4,
                      padding: 8, marginBottom: 10,
                    }}>
                      {/* A draft_reply preview is the agent's prose reply → render
                          it as markdown (matches the Agent output below). Runnable
                          arg previews are key=value lines, so keep those plain. */}
                      {p.tool_name === 'draft_reply' ? (
                        <Markdown color={textColor(isDark)}>{preview}</Markdown>
                      ) : (
                        <div style={{ whiteSpace: 'pre-wrap' }}>{preview}</div>
                      )}
                    </div>
                  )}
                  <Space>
                    <Button type="primary" icon={<CheckOutlined />} onClick={() => answer(card.card_id, true)}>Approve</Button>
                    <Button danger icon={<CloseOutlined />} onClick={() => answer(card.card_id, false)}>Deny</Button>
                  </Space>
                </div>
              )}

              {draft && (
                <div style={{
                  border: `1px solid ${COLUMN_COLOR.done}`, borderRadius: 6,
                  padding: 12, marginBottom: 16,
                  display: 'flex', alignItems: 'center', gap: 12,
                }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ color: COLUMN_COLOR.done, fontWeight: 600 }}>
                      Draft reply saved to Outlook{draft.to ? ` · to ${draft.to}` : ''}
                    </div>
                    <div style={{ color: dim, fontSize: 12 }}>
                      {draft.sent
                        ? 'Sent.'
                        : 'Review it in Outlook, then send — or send it from here.'}
                    </div>
                  </div>
                  {draft.sent ? (
                    <Tag color="green">Sent</Tag>
                  ) : (
                    <Button
                      type="primary" icon={<SendOutlined />} loading={draft.sending}
                      onClick={() => sendDraft(card.card_id)}
                    >
                      Send reply
                    </Button>
                  )}
                </div>
              )}

              {error && (
                <div style={{ color: COLUMN_COLOR.failed, marginBottom: 16, whiteSpace: 'pre-wrap' }}>{error}</div>
              )}

              {body && (
                <>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                    <span style={{ color: dim, fontSize: 12 }}>Agent output</span>
                    <CopyButton text={body} title="Copy agent output" />
                  </div>
                  <div style={{ marginBottom: 16 }}>
                    <Markdown color={textColor(isDark)}>{body}</Markdown>
                  </div>
                </>
              )}

              {card.timeline && card.timeline.length > 0 && (
                <>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                    <span style={{ color: dim, fontSize: 12 }}>Timeline</span>
                    <CopyButton
                      text={card.timeline.map(e => `${fmtTime(e.ts)}  ${e.text}`).join('\n')}
                      title="Copy timeline"
                    />
                  </div>
                  <div style={{ borderLeft: `2px solid ${border}`, paddingLeft: 10 }}>
                    {card.timeline.map((entry, i) => (
                      <div key={i} style={{ marginBottom: 3 }}>
                        <span style={{ color: dim, fontFamily: 'monospace', marginRight: 8 }}>{fmtTime(entry.ts)}</span>
                        {entry.text}
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>
          )
        })()}
      </Drawer>

      {/* Conversation pane — the selected queue card's transcript + reply box, a
          persistent pane under the inbox (this is where the old Rooms tab's chat
          now lives). Approve/Deny for a request still rides the right Drawer. */}
      {convoCard && (
        <div style={{ marginTop: 16, border: `1px solid ${border}`, borderRadius: 6, display: 'flex', flexDirection: 'column', maxHeight: 440 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', borderBottom: `1px solid ${border}` }}>
            <span style={{ fontWeight: 600 }}>{visitorOf(convoRoom)}</span>
            <span style={{ color: dim }}>· {convoCard.detail?.queue as string}</span>
            <Space style={{ marginLeft: 'auto' }}>
              <Button size="small" type="text" icon={<CloseOutlined />} onClick={() => setConvo(null)} title="Hide conversation" />
            </Space>
          </div>
          <div style={{ flex: 1, overflowY: 'auto', padding: '10px 12px', minHeight: 120 }}>
            {(messages[convoRoom] || []).length === 0 ? (
              <div style={{ color: dim, fontSize: 12 }}>No messages yet.</div>
            ) : (messages[convoRoom] || []).map((m, i) => (
              <div key={m.id || i} style={{ display: 'flex', gap: 8, padding: '3px 0', alignItems: 'baseline', fontSize: 13 }}>
                <span style={{ fontWeight: 600, color: dim, whiteSpace: 'nowrap' }}>{m.sender || visitorOf(convoRoom)}</span>
                {m.kind === 'request' && <Tag color="purple" style={{ fontSize: 10 }}>request</Tag>}
                <span style={{ flex: 1, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{m.text}</span>
                <span style={{ color: dim, fontSize: 11, whiteSpace: 'nowrap' }}>{m.ts ? fmtTime(m.ts) : ''}</span>
              </div>
            ))}
          </div>
          <div style={{ display: 'flex', gap: 8, padding: '10px 12px', borderTop: `1px solid ${border}` }}>
            <Input.TextArea
              value={convoDraft[convoRoom] || ''}
              onChange={e => setConvoDraft(prev => ({ ...prev, [convoRoom]: e.target.value }))}
              onPressEnter={e => { if (!e.shiftKey) { e.preventDefault(); sendReply(convoRoom) } }}
              placeholder={`Reply to ${visitorOf(convoRoom)}… (Enter to send, Shift+Enter for newline)`}
              autoSize={{ minRows: 1, maxRows: 4 }}
            />
            <Button type="primary" icon={<SendOutlined />} loading={sending} onClick={() => sendReply(convoRoom)} disabled={!(convoDraft[convoRoom] || '').trim()}>Send</Button>
          </div>
        </div>
      )}

      {/* Event audit feed — open by default so the trail (every event seen, its
          disposition) is visible without a click; collapsible to free space. */}
      <Collapse
        size="small"
        defaultActiveKey={['events']}
        style={{ marginTop: 20 }}
        items={[{
          key: 'events',
          label: (
            <span><BellOutlined /> Event log<span style={{ color: dim, fontSize: 12 }}> · every event seen, matched or not</span></span>
          ),
          children: events.length === 0 ? (
            <div style={{ color: dim, fontSize: 12 }}>No events yet.</div>
          ) : (
            <div style={{ maxHeight: 260, overflowY: 'auto', border: `1px solid ${border}`, borderRadius: 6 }}>
            {events.map((ev, i) => {
              const disposition =
                ev.matched.length > 0
                  ? { color: COLUMN_COLOR.done, text: `fired: ${ev.matched.join(', ')}` }
                  : ev.skipped.length > 0
                    ? { color: COLUMN_COLOR.awaiting_human, text: `skipped (working hours): ${ev.skipped.join(', ')}` }
                    : { color: dim, text: 'no match' }
              return (
                <div
                  key={ev.id || i}
                  style={{
                    display: 'flex', gap: 8, alignItems: 'baseline', padding: '4px 8px',
                    borderBottom: i < events.length - 1 ? `1px solid ${border}` : undefined,
                    fontSize: 12,
                  }}
                >
                  <span style={{ color: dim, fontFamily: 'monospace', flexShrink: 0 }}>{fmtTime(ev.ts)}</span>
                  <span style={{ color: dim, flexShrink: 0 }}>{ev.source}:{ev.type}</span>
                  <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{ev.summary}</span>
                  <span style={{ color: disposition.color, flexShrink: 0 }}>{disposition.text}</span>
                </div>
              )
            })}
            </div>
          ),
        }]}
      />
    </div>
  )
}

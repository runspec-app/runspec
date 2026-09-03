import { useEffect, useRef, useState } from 'react'
import { Tag, Alert, Button, Tooltip, Space, Collapse, Input, message } from 'antd'
import { CheckOutlined, CloseOutlined, ReloadOutlined, BellOutlined, SendOutlined } from '@ant-design/icons'
import { bridge, type TriggerCard, type TriggerEvent, type TriggerListResult } from '../bridge'
import { useIsDark, textColor, textMuted } from '../ThemeContext'
import { Markdown } from '../components/Markdown'
import { RunInbox, type CardTranscript } from '../components/RunInbox'
import { EventLog } from '../components/EventLog'
import { TriggersEditor } from '../components/TriggersEditor'
import { CopyButton, COLUMN_COLOR } from '../components/runShared'

// A chat card is a helpdesk-room thread surfaced in the Triggers inbox (source
// === 'chat'); clicking it opens the live conversation pane. A portal card
// (source === 'portal') is a runnables-portal request the hub routed to this
// console — it opens the detail drawer, where the autonomy-gate Approve/Deny and
// the enactment result render (the claim-free request lifecycle, no claim
// controls). See docs/design/runnables-portal.md.
type QueueMsg = { sender?: string; text?: string; id?: string; ts?: number; kind?: string }
const isChatCard = (c: TriggerCard) => c.source === 'chat'
const visitorOf = (room: string) => room.split('/').slice(2).join('/') || room

function fmtTime(epochSeconds: number): string {
  if (!epochSeconds) return ''
  return new Date(epochSeconds * 1000).toLocaleTimeString()
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

export function TriggersView() {
  const isDark = useIsDark()
  const [cards, setCards] = useState<Record<string, TriggerCard>>({})
  const [pending, setPending] = useState<Record<string, PendingConfirm>>({})
  const [config, setConfig] = useState<TriggerListResult | null>(null)
  // Streamed agent text per card id (its reasoning/reply as it arrives). Kept as a
  // flat string for copy + the finalised-result fallback.
  const [streams, setStreams] = useState<Record<string, string>>({})
  // Rich per-card transcript: interleaved text + tool-call segments, built from the
  // same token/tool_start/tool_end events the console uses, so a live agent
  // interaction renders with its tool calls (not just flat prose).
  const [transcripts, setTranscripts] = useState<Record<string, CardTranscript>>({})
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
    // Seed live gates parked before this view mounted (a re-opened / cold-start
    // console) from the authoritative backend snapshot — the live
    // runspec:tool_confirm event only fires for gates raised after mount, so
    // without this a pending confirm would be invisible until it timed out.
    bridge.get_pending_confirms().then(gates => {
      setPending(prev => {
        const next = { ...prev }
        for (const g of gates) {
          next[g.id] = {
            request_id: g.request_id,
            tool_name: g.tool_name,
            tool_input: g.tool_input ?? {},
            display_name: g.display_name,
            display_input: g.display_input,
          }
        }
        return next
      })
    }).catch(console.error)
  }, [])

  // Live updates from the bridge.
  useEffect(() => {
    const onCard = (e: Event) => {
      const card = (e as CustomEvent).detail as TriggerCard
      cardIds.current.add(card.card_id)
      setCards(prev => ({ ...prev, [card.card_id]: card }))
      // Reconcile the gate against the backend column: once a card leaves
      // 'awaiting_human' (approved, denied, or a confirm that timed out on the
      // backend), a still-pending gate for it is stale — drop it so a dead
      // request_id can't keep an Approve card up (mirrors App.tsx's badge
      // reconciliation). The runspec:confirm_resolved event handles the
      // request-id-precise case; this is the column-level backstop.
      if (card.column !== 'awaiting_human') {
        setPending(prev => {
          if (!prev[card.card_id]) return prev
          const next = { ...prev }; delete next[card.card_id]; return next
        })
      }
    }
    const onToken = (e: Event) => {
      const { id, token } = (e as CustomEvent).detail as { id: string; token: string }
      if (!cardIds.current.has(id)) return  // ignore main-chat tokens
      setStreams(s => ({ ...s, [id]: (s[id] ?? '') + token }))
      setTranscripts(prev => {
        const cur = prev[id] ?? { segments: [], currentText: '', done: false }
        return { ...prev, [id]: { ...cur, currentText: cur.currentText + token } }
      })
    }
    const onToolStart = (e: Event) => {
      const { id, tool_name, tool_input } = (e as CustomEvent).detail as {
        id: string; tool_name: string; tool_input: Record<string, unknown>
      }
      if (!cardIds.current.has(id)) return
      setTranscripts(prev => {
        const cur = prev[id] ?? { segments: [], currentText: '', done: false }
        return {
          ...prev,
          [id]: {
            ...cur,
            segments: [
              ...cur.segments,
              ...(cur.currentText ? [{ kind: 'text' as const, text: cur.currentText }] : []),
              { kind: 'tool' as const, entry: { name: tool_name, input: tool_input, running: true, startedAt: Date.now() } },
            ],
            currentText: '',
          },
        }
      })
    }
    const onToolEnd = (e: Event) => {
      const { id, tool_name, output } = (e as CustomEvent).detail as {
        id: string; tool_name: string; output: string
      }
      if (!cardIds.current.has(id)) return
      setTranscripts(prev => {
        const cur = prev[id]
        if (!cur) return prev
        return {
          ...prev,
          [id]: {
            ...cur,
            segments: cur.segments.map(seg => {
              if (seg.kind === 'tool' && seg.entry.name === tool_name && seg.entry.running) {
                const durationMs = seg.entry.startedAt ? Date.now() - seg.entry.startedAt : undefined
                return { ...seg, entry: { ...seg.entry, output, running: false, durationMs } }
              }
              return seg
            }),
          },
        }
      })
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
    const onResolved = (e: Event) => {
      // A confirm gate finished on the backend — approved, denied, or timed out.
      // Clear this card's gate iff it's the request_id that just resolved, so a
      // late/stale confirm can't leave a dead Approve card that wedges the run at
      // 'waiting for human'. request_id-precise: a newer gate (different id) for
      // the same card is left untouched.
      const { id, request_id } = (e as CustomEvent).detail as { id: string; request_id: string }
      setPending(p => {
        if (!p[id] || p[id].request_id !== request_id) return p
        const next = { ...p }; delete next[id]; return next
      })
    }
    const onEnd = (e: Event) => {
      const { id } = (e as CustomEvent).detail as { id: string }
      setPending(p => {
        if (!p[id]) return p
        const next = { ...p }; delete next[id]; return next
      })
      // Flush the live tail into a committed text segment and mark the turn done.
      setTranscripts(prev => {
        const cur = prev[id]
        if (!cur) return prev
        return {
          ...prev,
          [id]: {
            segments: cur.currentText ? [...cur.segments, { kind: 'text', text: cur.currentText }] : cur.segments,
            currentText: '',
            done: true,
          },
        }
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
    window.addEventListener('runspec:tool_start', onToolStart)
    window.addEventListener('runspec:tool_end', onToolEnd)
    window.addEventListener('runspec:tool_confirm', onConfirm)
    window.addEventListener('runspec:confirm_resolved', onResolved)
    window.addEventListener('runspec:run_end', onEnd)
    window.addEventListener('runspec:trigger_status', onStatus)
    // Agent-driven config edits (manage_triggers / manage_rota) — the view
    // stays mounted behind display:none, so it must refetch on the event.
    window.addEventListener('runspec:triggers_updated', onStatus)
    window.addEventListener('runspec:room_message', onMessage)
    window.addEventListener('runspec:room_draft', onRoomDraft)
    window.addEventListener('runspec:trigger_event', onEvent)
    window.addEventListener('runspec:trigger_draft', onDraft)
    return () => {
      window.removeEventListener('runspec:trigger_card', onCard)
      window.removeEventListener('runspec:token', onToken)
      window.removeEventListener('runspec:tool_start', onToolStart)
      window.removeEventListener('runspec:tool_end', onToolEnd)
      window.removeEventListener('runspec:tool_confirm', onConfirm)
      window.removeEventListener('runspec:confirm_resolved', onResolved)
      window.removeEventListener('runspec:run_end', onEnd)
      window.removeEventListener('runspec:trigger_status', onStatus)
      window.removeEventListener('runspec:triggers_updated', onStatus)
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

  // Shared-mailbox claim: which cards have a claim in flight (button spinner).
  const [claiming, setClaiming] = useState<Record<string, boolean>>({})
  const claimEmail = async (cardId: string) => {
    if (claiming[cardId]) return
    setClaiming(prev => ({ ...prev, [cardId]: true }))
    try {
      const res = await bridge.claim_email(cardId)
      if (res.claimed) message.success('Claimed — running now.')
      else if (res.error) message.error(res.error)
      else message.info(res.owner ? `Already claimed by ${res.owner}.` : 'Already claimed on another console.')
    } catch (err) {
      console.error(err)
      message.error('Could not claim this email.')
    } finally {
      setClaiming(prev => { const next = { ...prev }; delete next[cardId]; return next })
    }
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

  // awaiting_human is overlaid client-side when a card has a parked confirm gate.
  const statusOf = (card: TriggerCard) =>
    pending[card.card_id] ? 'awaiting_human' : card.column
  // A chat card opens the conversation pane; everything else opens the drawer.
  const onSelect = (id: string | null) => {
    if (id === null) { setSelected(null); return }
    if (isChatCard(cards[id])) setConvo(id)
    else setSelected(id)
  }
  const convoCard = convo ? cards[convo] : undefined
  const convoRoom = (convoCard?.detail?.room as string | undefined) || ''

  const dim = textMuted(isDark)
  const colBg = isDark ? '#141414' : '#f0f0f0'
  const border = isDark ? '#303030' : '#e8e8e8'

  const sources = config?.sources ?? []
  const triggers = config?.triggers ?? []
  const errors = config?.errors ?? []

  // Only warn about an unavailable source that a configured trigger actually
  // uses — no point nagging about, say, the notifications source on a machine
  // that only runs Outlook triggers.
  const usedSources = new Set(triggers.map(t => String(t.source)))

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

      {/* Configured triggers — collapsible editor (create/edit/delete + Trigger now) */}
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
          children: <TriggersEditor triggers={triggers} sources={sources} reload={loadConfig} />,
        }]}
      />

      {/* Runs — shared inbox list + detail drawer. The trigger-only sections
          (portal request, approve/deny gate, draft reply) are injected here; the
          chat conversation pane stays below. */}
      <RunInbox
        cards={cards}
        selected={selected}
        onSelect={onSelect}
        onStop={id => bridge.cancel_chat(id).catch(console.error)}
        statusOf={statusOf}
        streams={streams}
        transcripts={transcripts}
        emptyText="No trigger runs yet."
        renderRowExtra={card => (
          card.column === 'awaiting_claim' ? (
            <Button
              size="small" type="primary" loading={!!claiming[card.card_id]}
              onClick={e => { e.stopPropagation(); claimEmail(card.card_id) }}
            >
              Claim
            </Button>
          ) : pending[card.card_id] ? (
            <Button
              size="small" type="primary"
              onClick={e => { e.stopPropagation(); setSelected(card.card_id) }}
            >
              Review
            </Button>
          ) : null
        )}
        renderDrawerExtra={card => {
          const p = pending[card.card_id]
          const preview = p ? describeToolInput(p.display_name ?? p.tool_name, p.display_input ?? p.tool_input) : ''
          const draft = drafts[card.card_id]
          return (
            <>
              {/* A portal request the hub routed here: what the visitor asked for. */}
              {card.source === 'portal' ? (
                <div style={{ marginBottom: 16, border: `1px solid ${border}`, borderRadius: 6, padding: 12 }}>
                  <div style={{ color: dim, fontSize: 12, marginBottom: 4 }}>
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

              {/* Shared-mailbox claim: this card is parked until an operator takes
                  it. Claiming moves the email to the claim folder and runs the
                  action; the poll flips it to "claimed by X" if another console won. */}
              {card.column === 'awaiting_claim' && (
                <div style={{
                  border: `1px solid ${COLUMN_COLOR.awaiting_human}`, borderRadius: 6,
                  padding: 12, marginBottom: 16,
                  display: 'flex', alignItems: 'center', gap: 12,
                }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ color: COLUMN_COLOR.awaiting_human, fontWeight: 600 }}>
                      Awaiting claim
                    </div>
                    <div style={{ color: dim, fontSize: 12 }}>
                      Claim to take this email — it moves to{' '}
                      <code>{String(card.detail?.claim_folder ?? 'the claim folder')}</code>{' '}
                      and this trigger runs. Only one console wins.
                    </div>
                  </div>
                  <Button
                    type="primary" loading={!!claiming[card.card_id]}
                    onClick={() => claimEmail(card.card_id)}
                  >
                    Claim
                  </Button>
                </div>
              )}

              {p && (
                <div style={{
                  // Pinned to the bottom of the scrolling drawer so it stays in
                  // view as the agent transcript streams below — the gate used to
                  // sit at the top and scroll out of sight, easy to miss. Opaque
                  // background + shadow so streaming text doesn't bleed through.
                  position: 'sticky', bottom: 8, zIndex: 3,
                  background: colBg,
                  boxShadow: `0 -6px 16px -8px ${isDark ? 'rgba(0,0,0,0.7)' : 'rgba(0,0,0,0.25)'}`,
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
            </>
          )
        }}
      />

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

      {/* Event audit feed — every event seen + its disposition (shared widget). */}
      <EventLog events={events} />
    </div>
  )
}

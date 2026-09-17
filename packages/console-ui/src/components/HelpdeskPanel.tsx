import { useEffect, useRef, useState } from 'react'
import { Tag, Button, Input, Space, message } from 'antd'
import { CloseOutlined, SendOutlined } from '@ant-design/icons'
import { bridge, type TriggerCard } from '../bridge'
import { useFeatures } from '../features'
import { useIsDark, textColor, textMuted, MONO_FONT, RADIUS } from '../ThemeContext'
import { COLUMN_COLOR } from './runShared'

/**
 * HelpdeskPanel — the helpdesk / queue side of the Triggers tab.
 *
 * When the console staffs a room queue (`room_queue` on a host), a trigger fire
 * can need queue-level handling the generic run stream doesn't cover: claiming a
 * shared-mailbox email, a runnables-portal request routed from the hub, an
 * agent-drafted reply awaiting Send, and the live visitor conversation. Those
 * used to live in the Triggers tab's RunInbox drawer render-props; with the run
 * activity + Approve/Deny gate now in the Console tab, this panel keeps just the
 * queue affordances, as a compact list + conversation pane.
 *
 * AI-on only (a queue is an agent feature) and self-hiding when there is nothing
 * to handle, so a plain rule-trigger user never sees it.
 */

type QueueMsg = { sender?: string; text?: string; id?: string; ts?: number; kind?: string }
const isChatCard = (c: TriggerCard) => c.source === 'chat'
const visitorOf = (room: string) => room.split('/').slice(2).join('/') || room

function fmtTime(epochSeconds: number): string {
  if (!epochSeconds) return ''
  return new Date(epochSeconds * 1000).toLocaleTimeString()
}

type Draft = { entry_id: string; to: string; subject: string; sent?: boolean; sending?: boolean }

// A card this panel handles: a queue conversation, a portal request, a claim to
// take, or a run with a saved draft reply.
function isHelpdeskCard(card: TriggerCard, drafts: Record<string, Draft>): boolean {
  return card.source === 'chat' || card.source === 'portal'
    || card.column === 'awaiting_claim' || !!drafts[card.card_id]
}

export function HelpdeskPanel() {
  const { ai } = useFeatures()
  const isDark = useIsDark()
  const [cards, setCards] = useState<Record<string, TriggerCard>>({})
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  const [claiming, setClaiming] = useState<Record<string, boolean>>({})
  const [messages, setMessages] = useState<Record<string, QueueMsg[]>>({})
  const [convoDraft, setConvoDraft] = useState<Record<string, string>>({})
  const [convo, setConvo] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const cardIds = useRef<Set<string>>(new Set())

  useEffect(() => {
    bridge.get_trigger_board().then(board => {
      board.forEach(c => cardIds.current.add(c.card_id))
      // Merge (don't replace) so a card that arrived via runspec:trigger_card
      // between mount and this resolving isn't wiped.
      setCards(prev => ({ ...Object.fromEntries(board.map(c => [c.card_id, c])), ...prev }))
    }).catch(() => {})
  }, [])

  useEffect(() => {
    const onCard = (e: Event) => {
      const card = (e as CustomEvent).detail as TriggerCard
      cardIds.current.add(card.card_id)
      setCards(prev => ({ ...prev, [card.card_id]: card }))
    }
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
      const d = (e as CustomEvent).detail as { room?: string; text?: string }
      const room = d.room || ''
      if (!room.startsWith('helpdesk/')) return
      setConvoDraft(prev => ({ ...prev, [room]: d.text || '' }))
      // Bring the conversation the agent drafted into for review into view.
      const card = Object.values(cards).find(c => (c.detail?.room as string | undefined) === room)
      if (card) setConvo(card.card_id)
    }
    const onDraft = (e: Event) => {
      const { id, entry_id, to, subject } = (e as CustomEvent).detail as {
        id: string; entry_id: string; to: string; subject: string
      }
      if (entry_id) setDrafts(prev => ({ ...prev, [id]: { entry_id, to, subject } }))
    }
    window.addEventListener('runspec:trigger_card', onCard)
    window.addEventListener('runspec:room_message', onMessage)
    window.addEventListener('runspec:room_draft', onRoomDraft)
    window.addEventListener('runspec:trigger_draft', onDraft)
    return () => {
      window.removeEventListener('runspec:trigger_card', onCard)
      window.removeEventListener('runspec:room_message', onMessage)
      window.removeEventListener('runspec:room_draft', onRoomDraft)
      window.removeEventListener('runspec:trigger_draft', onDraft)
    }
  }, [cards])

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

  const sendDraft = async (cardId: string) => {
    const d = drafts[cardId]
    if (!d || d.sending || d.sent) return
    setDrafts(prev => ({ ...prev, [cardId]: { ...prev[cardId], sending: true } }))
    try {
      const res = await bridge.send_trigger_draft(d.entry_id)
      setDrafts(prev => ({ ...prev, [cardId]: { ...prev[cardId], sending: false, sent: !!res.ok } }))
    } catch (err) {
      console.error(err)
      setDrafts(prev => ({ ...prev, [cardId]: { ...prev[cardId], sending: false } }))
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

  const dim = textMuted(isDark)
  const border = 'var(--rs-border)'
  const helpdeskCards = Object.values(cards)
    .filter(c => isHelpdeskCard(c, drafts))
    .sort((a, b) => (a.created_at || 0) - (b.created_at || 0))
  const convoCard = convo ? cards[convo] : undefined
  const convoRoom = (convoCard?.detail?.room as string | undefined) || ''

  // Nothing to staff (and no AI) → render nothing, so a plain trigger user never
  // sees an empty helpdesk box.
  if (!ai || (helpdeskCards.length === 0 && !convoCard)) return null

  return (
    <div style={{ color: textColor(isDark), marginBottom: 20 }}>
      <div style={{ fontWeight: 600, marginBottom: 8 }}>Helpdesk queue</div>
      {helpdeskCards.length === 0 ? (
        <div style={{ color: dim, fontSize: 13 }}>No open queue items.</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {helpdeskCards.map(card => {
            const draft = drafts[card.card_id]
            const room = (card.detail?.room as string | undefined) || ''
            return (
              <div
                key={card.card_id}
                style={{ border: `1px solid ${border}`, borderRadius: RADIUS.sm, padding: 12 }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <span style={{ fontWeight: 600, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {card.summary || card.trigger}
                  </span>

                  {/* A queue conversation — open the pane below. */}
                  {isChatCard(card) && (
                    <Button size="small" onClick={() => setConvo(card.card_id)}>Open chat</Button>
                  )}

                  {/* Shared-mailbox claim — claiming moves the email to the claim
                      folder and runs the action; only one console wins. */}
                  {card.column === 'awaiting_claim' && (
                    <Space>
                      <Tag color="gold" style={{ margin: 0 }}>awaiting claim</Tag>
                      <Button
                        size="small" type="primary" loading={!!claiming[card.card_id]}
                        onClick={() => claimEmail(card.card_id)}
                      >
                        Claim
                      </Button>
                    </Space>
                  )}

                  {/* An agent-drafted reply awaiting Send. */}
                  {draft && (draft.sent ? (
                    <Tag color="green" style={{ margin: 0 }}>reply sent</Tag>
                  ) : (
                    <Button
                      size="small" type="primary" icon={<SendOutlined />} loading={draft.sending}
                      onClick={() => sendDraft(card.card_id)}
                    >
                      Send reply
                    </Button>
                  ))}
                </div>

                {/* A portal request the hub routed here: what the visitor asked for. */}
                {card.source === 'portal' && (
                  <div style={{ marginTop: 8, color: dim, fontSize: 13 }}>
                    <div style={{ marginBottom: 2 }}>
                      {card.detail?.visitor_name
                        ? `${String(card.detail.visitor_name)} <${String(card.detail?.visitor ?? '')}>`
                        : String(card.detail?.visitor ?? 'A visitor')}
                      {card.detail?.team ? (
                        <Tag color="geekblue" style={{ margin: '0 0 0 6px', fontSize: 11 }}>{String(card.detail.team)}</Tag>
                      ) : null}{' '}
                      requested
                    </div>
                    <div style={{ fontFamily: MONO_FONT, color: textColor(isDark) }}>{String(card.detail?.runnable ?? '')}</div>
                    {!!card.detail?.args && Object.keys(card.detail.args as object).length > 0 && (
                      <div style={{ whiteSpace: 'pre-wrap', marginTop: 2 }}>
                        {Object.entries(card.detail.args as Record<string, unknown>)
                          .map(([k, v]) => `${k} = ${typeof v === 'string' ? v : JSON.stringify(v)}`)
                          .join('\n')}
                      </div>
                    )}
                  </div>
                )}

                {draft && !draft.sent && (
                  <div style={{ marginTop: 6, color: dim, fontSize: 13 }}>
                    Draft reply saved to Outlook{draft.to ? ` · to ${draft.to}` : ''} — review it in Outlook, then send (or send from here).
                  </div>
                )}
                {card.column === 'awaiting_claim' && (
                  <div style={{ marginTop: 6, color: dim, fontSize: 13 }}>
                    Claim to take this email — it moves to{' '}
                    <code>{String(card.detail?.claim_folder ?? 'the claim folder')}</code>{' '}
                    and this trigger runs. Approve/deny of the resulting run happens in the Console tab.
                  </div>
                )}
                {room && <span style={{ display: 'none' }} data-room={room} />}
              </div>
            )
          })}
        </div>
      )}

      {/* Conversation pane — the selected queue card's transcript + reply box. */}
      {convoCard && (
        <div style={{ marginTop: 12, border: `1px solid ${border}`, borderRadius: RADIUS.sm, display: 'flex', flexDirection: 'column', maxHeight: 440 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', borderBottom: `1px solid ${border}` }}>
            <span style={{ fontWeight: 600 }}>{visitorOf(convoRoom)}</span>
            <span style={{ color: dim }}>· {convoCard.detail?.queue as string}</span>
            <Space style={{ marginLeft: 'auto' }}>
              <Button size="small" type="text" icon={<CloseOutlined />} onClick={() => setConvo(null)} title="Hide conversation" />
            </Space>
          </div>
          <div style={{ flex: 1, overflowY: 'auto', padding: '10px 12px', minHeight: 120 }}>
            {(messages[convoRoom] || []).length === 0 ? (
              <div style={{ color: dim, fontSize: 13 }}>No messages yet.</div>
            ) : (messages[convoRoom] || []).map((m, i) => (
              <div key={m.id || i} style={{ display: 'flex', gap: 8, padding: '3px 0', alignItems: 'baseline', fontSize: 14 }}>
                <span style={{ fontWeight: 600, color: dim, whiteSpace: 'nowrap' }}>{m.sender || visitorOf(convoRoom)}</span>
                {m.kind === 'request' && <Tag color="purple" style={{ fontSize: 11 }}>request</Tag>}
                <span style={{ flex: 1, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{m.text}</span>
                <span style={{ color: dim, fontSize: 12, whiteSpace: 'nowrap' }}>{m.ts ? fmtTime(m.ts) : ''}</span>
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
    </div>
  )
}

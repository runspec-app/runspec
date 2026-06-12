import { useEffect, useMemo, useRef, useState } from 'react'
import { Button, Empty, Input, Tag, Typography, message } from 'antd'
import { SendOutlined, RobotOutlined, FlagOutlined, CheckOutlined, UndoOutlined, CloseOutlined, DownOutlined, UpOutlined } from '@ant-design/icons'
import { bridge, type RoomInfo, type RoomMessage, type RoomPresence, type RoomConversation, type ThreadState } from '../bridge'
import { useIsDark } from '../ThemeContext'

const { Text } = Typography

// The Rooms view — the console's window into the team chat rooms. Each room is a
// property of a fleet host (room + room_port in runspec_hosts.toml); the console
// joins each one always-on over that host's SSH tunnel. This view lists the rooms,
// and shows the selected room's live transcript + presence + composer, all fed by
// the bridge's runspec:room_* events:
//   • room_message      → append to that room's transcript
//   • room_presence     → that room's presence list
//   • room_connection   → that room's connected flag (reconnect surfaces here)
//   • room_conversation → a helpdesk thread's lifecycle state (open/assigned/…)
//   • room_draft        → switch to the room + pre-fill the composer (agent's
//                         proposed reply — the human edits and sends; agent never does)
// A console drops the server's echo of its *own* messages, so we append our sent
// messages optimistically.
//
// **Helpdesk agent mode** (a host entry with room_queue): the entry is a queue
// the console staffs. Its panel is an INBOX of private visitor conversations
// (helpdesk/<queue>/<visitor>), each with a lifecycle chip and Claim / Resolve /
// Reopen / Close controls. Claiming a thread is also what releases the agent LLM
// turn for it — unclaimed threads are visible but never spend the model.

interface Msg {
  sender: string
  text: string
  id: string
  ts: number
  kind?: string
}

function fmtTime(ts: number): string {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

// helpdesk/<queue>/<visitor> → the visitor identity (the thread's human label).
function visitorOf(room: string): string {
  const parts = room.split('/')
  return parts.length === 3 ? parts[2] : room
}

const STATE_COLORS: Record<string, string> = {
  open: 'gold',
  assigned: 'blue',
  resolved: 'green',
  closed: 'default',
}

export function RoomsView({ active }: { active: boolean }) {
  const isDark = useIsDark()
  const [rooms, setRooms] = useState<RoomInfo[]>([])
  const [selected, setSelected] = useState<string>('')
  const [messages, setMessages] = useState<Record<string, Msg[]>>({})
  const [presence, setPresence] = useState<Record<string, string[]>>({})
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  // Helpdesk inbox: conversation room → lifecycle, and per-queue thread selection.
  const [threads, setThreads] = useState<Record<string, ThreadState>>({})
  const [threadSel, setThreadSel] = useState<Record<string, string>>({})
  const [sending, setSending] = useState(false)
  const [acting, setActing] = useState(false)
  const [inboxCollapsed, setInboxCollapsed] = useState(false)

  const transcriptRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<any>(null)

  // room name → this console's display name there (for "mine" styling + presence).
  const userByRoom = useMemo(() => {
    const m: Record<string, string> = {}
    for (const r of rooms) m[r.room] = r.user
    return m
  }, [rooms])

  const refreshState = () => bridge.get_room_state().then(s => {
    setRooms(s.rooms)
    // Seed the inbox with the threads the hub caught us up on at subscribe.
    setThreads(prev => {
      const next = { ...prev }
      for (const r of s.rooms) for (const [room, st] of Object.entries(r.threads || {})) next[room] = st
      return next
    })
    // Seed presence with ourselves for each connected room (the server never
    // echoes our own presence back).
    setPresence(prev => {
      const next = { ...prev }
      for (const r of s.rooms) {
        if (r.connected) next[r.room] = Array.from(new Set([...(next[r.room] || []), r.user]))
      }
      return next
    })
    setSelected(cur => cur || s.rooms.find(r => r.connected)?.room || s.rooms[0]?.room || '')
  })
  useEffect(() => { refreshState() }, [])
  useEffect(() => { if (active) refreshState() }, [active])

  // Subscribe to the room event stream. Stays mounted across tab switches, so all
  // rooms keep filling even when not visible.
  useEffect(() => {
    const onMessage = (e: Event) => {
      const m = (e as CustomEvent<RoomMessage>).detail
      setMessages(prev => {
        const list = prev[m.room] || []
        if (m.id && list.some(x => x.id === m.id)) return prev   // dedupe by id
        return { ...prev, [m.room]: [...list, { sender: m.sender, text: m.text, id: m.id, ts: m.ts, kind: m.kind }] }
      })
    }
    const onPresence = (e: Event) => {
      const p = (e as CustomEvent<RoomPresence>).detail
      setPresence(prev => {
        const without = (prev[p.room] || []).filter(u => u !== p.user)
        return { ...prev, [p.room]: p.status === 'available' ? [...without, p.user] : without }
      })
    }
    const onConversation = (e: Event) => {
      const c = (e as CustomEvent<RoomConversation>).detail
      setThreads(prev => ({ ...prev, [c.room]: { state: c.state, assignee: c.assignee } }))
    }
    const onConnection = (e: Event) => {
      const c = (e as CustomEvent<{ room: string; host: string; connected: boolean }>).detail
      setRooms(prev => prev.map(r => (r.room === c.room ? { ...r, connected: c.connected } : r)))
      if (!c.connected) {
        // Clear the room's presence (keep only ourselves) on a drop.
        setPresence(prev => ({ ...prev, [c.room]: (prev[c.room] || []).filter(u => u === userByRoom[c.room]) }))
      }
    }
    const onDraft = (e: Event) => {
      const d = (e as CustomEvent<{ id: string; room: string; text: string }>).detail
      setDrafts(prev => ({ ...prev, [d.room]: d.text }))
      // A draft for a helpdesk conversation selects its queue entry + the thread;
      // a group-chat draft selects the room directly.
      const ns = d.room.startsWith('helpdesk/') ? d.room.split('/').slice(0, 2).join('/') : ''
      if (ns) {
        setSelected(ns)
        setThreadSel(prev => ({ ...prev, [ns]: d.room }))
        message.info(`The agent proposed a reply to ${visitorOf(d.room)} — review and send it.`)
      } else {
        setSelected(d.room)
        message.info(`The agent proposed a reply in #${d.room} — review and send it.`)
      }
      setTimeout(() => inputRef.current?.focus?.(), 0)
    }
    window.addEventListener('runspec:room_message', onMessage)
    window.addEventListener('runspec:room_presence', onPresence)
    window.addEventListener('runspec:room_conversation', onConversation)
    window.addEventListener('runspec:room_connection', onConnection)
    window.addEventListener('runspec:room_draft', onDraft)
    return () => {
      window.removeEventListener('runspec:room_message', onMessage)
      window.removeEventListener('runspec:room_presence', onPresence)
      window.removeEventListener('runspec:room_conversation', onConversation)
      window.removeEventListener('runspec:room_connection', onConnection)
      window.removeEventListener('runspec:room_draft', onDraft)
    }
  }, [userByRoom])

  const current = rooms.find(r => r.room === selected)
  const isQueue = !!current?.queue
  // In queue mode the composer + transcript target the selected THREAD.
  const activeRoom = isQueue ? (threadSel[selected] || '') : selected
  const draft = drafts[activeRoom] || ''
  const setDraft = (v: string) => setDrafts(prev => ({ ...prev, [activeRoom]: v }))

  // Pin the transcript to the newest message when the active log grows.
  useEffect(() => {
    const el = transcriptRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, activeRoom])

  // The inbox: every conversation under this queue's namespace — the lifecycle
  // map (hub catch-up) unioned with any room we've seen messages for.
  const queueThreads = useMemo(() => {
    if (!isQueue || !current) return []
    const prefix = current.room + '/'
    const roomsSeen = new Set<string>([
      ...Object.keys(threads).filter(r => r.startsWith(prefix)),
      ...Object.keys(messages).filter(r => r.startsWith(prefix)),
    ])
    return [...roomsSeen].sort().map(room => ({
      room,
      visitor: visitorOf(room),
      state: threads[room]?.state || 'open',
      assignee: threads[room]?.assignee ?? null,
      last: (messages[room] || []).slice(-1)[0],
    }))
  }, [isQueue, current, threads, messages])

  const send = async () => {
    const text = draft.trim()
    if (!text || sending || !activeRoom) return
    setSending(true)
    try {
      const res = await bridge.send_room_message(activeRoom, text)
      if (!res.ok) { message.error(res.error || 'Could not send the message'); return }
      const me = userByRoom[selected] || 'me'
      setMessages(prev => ({
        ...prev,
        [activeRoom]: [...(prev[activeRoom] || []), { sender: me, text, id: 'local-' + Date.now(), ts: Date.now() / 1000 }],
      }))
      setDraft('')
    } catch (err) {
      message.error(String(err))
    } finally {
      setSending(false)
      inputRef.current?.focus?.()
    }
  }

  const control = async (kind: 'claim' | 'resolve' | 'reopen' | 'close') => {
    if (!activeRoom || acting) return
    setActing(true)
    try {
      const call = { claim: bridge.claim_thread, resolve: bridge.resolve_thread, reopen: bridge.reopen_thread, close: bridge.close_thread }[kind]
      const res = await call(activeRoom)
      if (!res.ok) message.error(res.error || `Could not ${kind} the thread`)
      // The new state arrives as a runspec:room_conversation broadcast.
    } catch (err) {
      message.error(String(err))
    } finally {
      setActing(false)
    }
  }

  const border = isDark ? '#303030' : '#e8e8e8'
  const panelBg = isDark ? '#1a1a1a' : '#fafafa'
  const accent = isDark ? '#4f9cf9' : '#1677ff'

  if (rooms.length === 0) {
    return (
      <div style={{ padding: 48, display: 'flex', justifyContent: 'center' }}>
        <Empty
          description={
            <span>
              No chat rooms.<br />
              Add <Text code>room</Text> (group chat) or <Text code>room_queue</Text> (help desk)
              plus <Text code>room_port</Text> to a host in your fleet (Hosts tab) — the console
              joins it automatically over that host's tunnel.
            </span>
          }
        />
      </div>
    )
  }

  const selfUser = current?.user || ''
  const msgs = messages[activeRoom] || []
  const present = presence[selected] || []
  const activeThread = isQueue ? queueThreads.find(t => t.room === activeRoom) : undefined
  const mineAssigned = activeThread?.assignee === selfUser
  const openCount = queueThreads.filter(t => t.state === 'open').length

  return (
    <div style={{ height: '100%', display: 'flex', minHeight: 0 }}>
      {/* Room list */}
      <div style={{ width: 180, flexShrink: 0, borderRight: `1px solid ${border}`, background: panelBg, overflowY: 'auto' }}>
        <div style={{ padding: '10px 14px', fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.05em', color: isDark ? '#8b93a3' : '#888' }}>
          Rooms
        </div>
        {rooms.map(r => (
          <div
            key={r.room}
            onClick={() => setSelected(r.room)}
            style={{
              padding: '8px 14px', cursor: 'pointer',
              background: r.room === selected ? (isDark ? '#22262e' : '#eef2f8') : 'transparent',
              borderLeft: r.room === selected ? `2px solid ${accent}` : '2px solid transparent',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{ color: r.connected ? '#3fb950' : '#888', fontSize: 10 }}>●</span>
              <Text strong style={{ fontSize: 13 }}>{r.queue ? `⛑ ${r.queue}` : `#${r.room}`}</Text>
            </div>
            <Text type="secondary" style={{ fontSize: 11 }}>{r.queue ? 'help desk · ' : ''}{r.host}</Text>
          </div>
        ))}
      </div>

      {/* Selected room / thread (in queue mode the inbox sits on top of it) */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        {/* Helpdesk inbox — full-width rows on top, so each row carries a real
            summary (status · visitor · assignee · last-message snippet · time).
            Collapses up to its one-line header to give the conversation the screen. */}
        {isQueue && (
          <div style={{ flexShrink: 0, display: 'flex', flexDirection: 'column', minHeight: 0, maxHeight: '45%', borderBottom: `1px solid ${border}`, background: panelBg }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 8px 6px 16px', flexShrink: 0 }}>
              <Text style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.05em', color: isDark ? '#8b93a3' : '#888' }}>
                Inbox · {queueThreads.length}{openCount > 0 ? ` · ${openCount} open` : ''}
              </Text>
              <Button type="text" size="small" icon={inboxCollapsed ? <DownOutlined /> : <UpOutlined />} onClick={() => setInboxCollapsed(c => !c)} title={inboxCollapsed ? 'Show inbox' : 'Hide inbox'} />
            </div>
            {!inboxCollapsed && (
              <div style={{ overflowY: 'auto', minHeight: 0 }}>
                {queueThreads.length === 0 ? (
                  <div style={{ padding: '4px 16px 10px' }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>No conversations yet.</Text>
                  </div>
                ) : queueThreads.map(t => (
                  <div
                    key={t.room}
                    onClick={() => setThreadSel(prev => ({ ...prev, [selected]: t.room }))}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 10, padding: '7px 16px', cursor: 'pointer',
                      background: t.room === activeRoom ? (isDark ? '#22262e' : '#eef2f8') : 'transparent',
                      borderLeft: t.room === activeRoom ? `2px solid ${accent}` : '2px solid transparent',
                    }}
                  >
                    <Tag color={STATE_COLORS[t.state] || 'default'} style={{ marginRight: 0, fontSize: 10, lineHeight: '16px', width: 66, textAlign: 'center' }}>{t.state}</Tag>
                    <Text strong style={{ fontSize: 13, width: 150, flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.visitor}</Text>
                    <Text type="secondary" style={{ fontSize: 11, width: 88, flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {t.assignee ? `→ ${t.assignee === selfUser ? 'you' : t.assignee}` : ''}
                    </Text>
                    <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 12, color: isDark ? '#8b93a3' : '#888' }}>
                      {t.last ? (t.last.kind === 'request' ? '⚙ ' : '') + t.last.text : '—'}
                    </span>
                    <Text type="secondary" style={{ fontSize: 11, flexShrink: 0, whiteSpace: 'nowrap' }}>{t.last ? fmtTime(t.last.ts) : ''}</Text>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 16px', borderBottom: `1px solid ${border}`, flexShrink: 0 }}>
          {isQueue ? (
            <>
              <Text strong style={{ fontSize: 14 }}>{activeThread ? visitorOf(activeRoom) : `⛑ ${current?.queue}`}</Text>
              {activeThread && (
                <Tag color={STATE_COLORS[activeThread.state] || 'default'}>
                  {activeThread.state}{activeThread.assignee ? ` · ${activeThread.assignee === selfUser ? 'you' : activeThread.assignee}` : ''}
                </Tag>
              )}
              <span style={{ flex: 1 }} />
              {activeThread && (
                <>
                  {(activeThread.state === 'open' || (activeThread.state === 'assigned' && !mineAssigned)) && (
                    <Button size="small" type="primary" icon={<FlagOutlined />} loading={acting} onClick={() => control('claim')}>
                      Claim
                    </Button>
                  )}
                  {activeThread.state === 'assigned' && mineAssigned && (
                    <Button size="small" icon={<CheckOutlined />} loading={acting} onClick={() => control('resolve')}>
                      Resolve
                    </Button>
                  )}
                  {(activeThread.state === 'resolved' || activeThread.state === 'closed') && (
                    <Button size="small" icon={<UndoOutlined />} loading={acting} onClick={() => control('reopen')}>
                      Reopen
                    </Button>
                  )}
                  {activeThread.state !== 'closed' && (
                    <Button size="small" danger icon={<CloseOutlined />} loading={acting} onClick={() => control('close')}>
                      Close
                    </Button>
                  )}
                </>
              )}
            </>
          ) : (
            <>
              <Text strong style={{ fontSize: 14 }}>#{selected}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>on {current?.host}</Text>
              <span style={{ flex: 1 }} />
            </>
          )}
          <Text type="secondary" style={{ fontSize: 12 }}>
            {current?.connected ? 'connected' : 'offline — reconnecting…'}
          </Text>
        </div>

        <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
          <div ref={transcriptRef} style={{ flex: 1, overflowY: 'auto', padding: '12px 16px' }}>
            {isQueue && !activeThread ? (
              <div style={{ marginTop: 40 }}>
                <Empty description="Pick a conversation from the inbox" image={Empty.PRESENTED_IMAGE_SIMPLE} />
              </div>
            ) : msgs.length === 0 ? (
              <div style={{ marginTop: 40 }}>
                <Empty description="No messages yet" image={Empty.PRESENTED_IMAGE_SIMPLE} />
              </div>
            ) : msgs.map((m, i) => (
              <div key={m.id || i} style={{ display: 'flex', gap: 8, padding: '3px 0', alignItems: 'baseline' }}>
                <Text strong style={{ color: m.sender === selfUser ? '#3fb950' : accent, whiteSpace: 'nowrap' }}>{m.sender}</Text>
                {m.kind === 'request' ? (
                  <span style={{ flex: 1 }}>
                    <Tag color="purple" style={{ fontSize: 10, lineHeight: '16px' }}>request</Tag>
                    <Text style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{m.text}</Text>
                  </span>
                ) : (
                  <Text style={{ flex: 1, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{m.text}</Text>
                )}
                <Text type="secondary" style={{ fontSize: 11, whiteSpace: 'nowrap' }}>{fmtTime(m.ts)}</Text>
              </div>
            ))}
          </div>

          {/* Presence is meaningful for group chat (who's in the room); a
              helpdesk thread is 1:1 (you + the visitor), so it's hidden there to
              give the conversation the width. */}
          {!isQueue && (
            <div style={{ width: 160, flexShrink: 0, borderLeft: `1px solid ${border}`, background: panelBg, padding: '12px 14px', overflowY: 'auto' }}>
              <Text type="secondary" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                Present · {present.length}
              </Text>
              <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 4 }}>
                {present.length === 0
                  ? <Text type="secondary" style={{ fontSize: 12 }}>—</Text>
                  : [...present].sort().map(u => (
                      <div key={u} style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ color: '#3fb950', fontSize: 10 }}>●</span>
                        <Text style={{ fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {u}{u === selfUser ? ' (you)' : ''}
                        </Text>
                      </div>
                    ))}
              </div>
            </div>
          )}
        </div>

        <div style={{ display: 'flex', gap: 8, padding: '12px 16px', borderTop: `1px solid ${border}`, flexShrink: 0 }}>
          <Input.TextArea
            ref={inputRef}
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onPressEnter={e => { if (!e.shiftKey) { e.preventDefault(); send() } }}
            placeholder={
              !current?.connected ? 'Offline — reconnecting to the room…'
              : isQueue && !activeThread ? 'Pick a conversation to reply…'
              : isQueue ? `Reply to ${visitorOf(activeRoom)}… (Enter to send, Shift+Enter for a newline)`
              : `Message #${selected}… (Enter to send, Shift+Enter for a newline)`
            }
            autoSize={{ minRows: 1, maxRows: 4 }}
            disabled={!current?.connected || (isQueue && !activeThread)}
          />
          <Button type="primary" icon={<SendOutlined />} onClick={send} loading={sending} disabled={!current?.connected || !draft.trim() || (isQueue && !activeThread)}>
            Send
          </Button>
        </div>
        <div style={{ padding: '0 16px 8px', marginTop: -4 }}>
          <Text type="secondary" style={{ fontSize: 11 }}>
            <RobotOutlined /> {isQueue
              ? 'Tip: claim a thread to own it — that also lets the agent draft replies for it. You always review and send.'
              : 'Tip: the agent can propose replies here — you always review and send.'}
          </Text>
        </div>
      </div>
    </div>
  )
}

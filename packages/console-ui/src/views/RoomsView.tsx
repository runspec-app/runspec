import { useEffect, useMemo, useRef, useState } from 'react'
import { Button, Empty, Input, Typography, message } from 'antd'
import { SendOutlined, RobotOutlined } from '@ant-design/icons'
import { bridge, type RoomInfo, type RoomMessage, type RoomPresence } from '../bridge'
import { useIsDark } from '../ThemeContext'

const { Text } = Typography

// The Rooms view — the console's window into the team chat rooms. Each room is a
// property of a fleet host (room + room_port in runspec_hosts.toml); the console
// joins each one always-on over that host's SSH tunnel. This view lists the rooms,
// and shows the selected room's live transcript + presence + composer, all fed by
// the bridge's runspec:room_* events:
//   • room_message    → append to that room's transcript
//   • room_presence   → that room's presence list
//   • room_connection → that room's connected flag (reconnect surfaces here)
//   • room_draft      → switch to the room + pre-fill the composer (agent's
//                       proposed reply — the human edits and sends; agent never does)
// A console drops the server's echo of its *own* messages, so we append our sent
// messages optimistically.

interface Msg {
  sender: string
  text: string
  id: string
  ts: number
}

function fmtTime(ts: number): string {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function RoomsView({ active }: { active: boolean }) {
  const isDark = useIsDark()
  const [rooms, setRooms] = useState<RoomInfo[]>([])
  const [selected, setSelected] = useState<string>('')
  const [messages, setMessages] = useState<Record<string, Msg[]>>({})
  const [presence, setPresence] = useState<Record<string, string[]>>({})
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [sending, setSending] = useState(false)

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
        return { ...prev, [m.room]: [...list, { sender: m.sender, text: m.text, id: m.id, ts: m.ts }] }
      })
    }
    const onPresence = (e: Event) => {
      const p = (e as CustomEvent<RoomPresence>).detail
      setPresence(prev => {
        const without = (prev[p.room] || []).filter(u => u !== p.user)
        return { ...prev, [p.room]: p.status === 'available' ? [...without, p.user] : without }
      })
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
      setSelected(d.room)
      message.info(`The agent proposed a reply in #${d.room} — review and send it.`)
      setTimeout(() => inputRef.current?.focus?.(), 0)
    }
    window.addEventListener('runspec:room_message', onMessage)
    window.addEventListener('runspec:room_presence', onPresence)
    window.addEventListener('runspec:room_connection', onConnection)
    window.addEventListener('runspec:room_draft', onDraft)
    return () => {
      window.removeEventListener('runspec:room_message', onMessage)
      window.removeEventListener('runspec:room_presence', onPresence)
      window.removeEventListener('runspec:room_connection', onConnection)
      window.removeEventListener('runspec:room_draft', onDraft)
    }
  }, [userByRoom])

  // Pin the transcript to the newest message when the selected room's log grows.
  useEffect(() => {
    const el = transcriptRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, selected])

  const current = rooms.find(r => r.room === selected)
  const draft = drafts[selected] || ''
  const setDraft = (v: string) => setDrafts(prev => ({ ...prev, [selected]: v }))

  const send = async () => {
    const text = draft.trim()
    if (!text || sending || !selected) return
    setSending(true)
    try {
      const res = await bridge.send_room_message(selected, text)
      if (!res.ok) { message.error(res.error || 'Could not send the message'); return }
      const me = userByRoom[selected] || 'me'
      setMessages(prev => ({
        ...prev,
        [selected]: [...(prev[selected] || []), { sender: me, text, id: 'local-' + Date.now(), ts: Date.now() / 1000 }],
      }))
      setDraft('')
    } catch (err) {
      message.error(String(err))
    } finally {
      setSending(false)
      inputRef.current?.focus?.()
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
              Add <Text code>room</Text> and <Text code>room_port</Text> to a host in your
              fleet (Hosts tab) — the console joins it automatically over that host's tunnel.
            </span>
          }
        />
      </div>
    )
  }

  const selfUser = current?.user || ''
  const msgs = messages[selected] || []
  const present = presence[selected] || []

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
              <Text strong style={{ fontSize: 13 }}>#{r.room}</Text>
            </div>
            <Text type="secondary" style={{ fontSize: 11 }}>{r.host}</Text>
          </div>
        ))}
      </div>

      {/* Selected room */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 16px', borderBottom: `1px solid ${border}`, flexShrink: 0 }}>
          <Text strong style={{ fontSize: 14 }}>#{selected}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>on {current?.host}</Text>
          <span style={{ flex: 1 }} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            {current?.connected ? 'connected' : 'offline — reconnecting…'}
          </Text>
        </div>

        <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
          <div ref={transcriptRef} style={{ flex: 1, overflowY: 'auto', padding: '12px 16px' }}>
            {msgs.length === 0 ? (
              <div style={{ marginTop: 40 }}>
                <Empty description="No messages yet" image={Empty.PRESENTED_IMAGE_SIMPLE} />
              </div>
            ) : msgs.map((m, i) => (
              <div key={m.id || i} style={{ display: 'flex', gap: 8, padding: '3px 0', alignItems: 'baseline' }}>
                <Text strong style={{ color: m.sender === selfUser ? '#3fb950' : accent, whiteSpace: 'nowrap' }}>{m.sender}</Text>
                <Text style={{ flex: 1, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{m.text}</Text>
                <Text type="secondary" style={{ fontSize: 11, whiteSpace: 'nowrap' }}>{fmtTime(m.ts)}</Text>
              </div>
            ))}
          </div>

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
                      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {u}{u === selfUser ? ' (you)' : ''}
                      </span>
                    </div>
                  ))}
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', gap: 8, padding: '12px 16px', borderTop: `1px solid ${border}`, flexShrink: 0 }}>
          <Input.TextArea
            ref={inputRef}
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onPressEnter={e => { if (!e.shiftKey) { e.preventDefault(); send() } }}
            placeholder={current?.connected ? `Message #${selected}… (Enter to send, Shift+Enter for a newline)` : 'Offline — reconnecting to the room…'}
            autoSize={{ minRows: 1, maxRows: 4 }}
            disabled={!current?.connected}
          />
          <Button type="primary" icon={<SendOutlined />} onClick={send} loading={sending} disabled={!current?.connected || !draft.trim()}>
            Send
          </Button>
        </div>
        <div style={{ padding: '0 16px 8px', marginTop: -4 }}>
          <Text type="secondary" style={{ fontSize: 11 }}>
            <RobotOutlined /> Tip: the agent can propose replies here — you always review and send.
          </Text>
        </div>
      </div>
    </div>
  )
}

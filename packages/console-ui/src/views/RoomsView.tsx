import { useEffect, useRef, useState } from 'react'
import { Button, Empty, Input, Tag, Tooltip, Typography, message } from 'antd'
import { SendOutlined, RobotOutlined, WifiOutlined } from '@ant-design/icons'
import { bridge, type RoomMessage, type RoomPresence, type RoomState } from '../bridge'
import { useIsDark } from '../ThemeContext'

const { Text } = Typography

// The Rooms view — the console's window into the team chat room (the coordination
// surface from the design). It renders the live transcript + presence and the
// composer, all fed by the bridge's runspec:room_* events:
//   • room_message  → append to the transcript
//   • room_presence → update the presence list
//   • room_draft    → pre-fill the composer (the agent's proposed reply, for the
//                     human to edit and send — the agent never sends)
// The composer's send is the one outbound path (bridge.send_room_message). A
// console drops the server's echo of its *own* messages (so it can't re-react to
// them), so our sent messages never come back as events — we append them
// optimistically here.

interface Msg {
  sender: string
  text: string
  id: string
  ts: number
  mine?: boolean
}

function fmtTime(ts: number): string {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function RoomsView({ active }: { active: boolean }) {
  const isDark = useIsDark()
  const [state, setState] = useState<RoomState>({ connected: false, room: '', user: '' })
  const [messages, setMessages] = useState<Msg[]>([])
  const [presence, setPresence] = useState<string[]>([])
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const stateRef = useRef(state)
  stateRef.current = state
  const transcriptRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<any>(null)

  // Load connection state, and include ourselves in the presence list (the server
  // never echoes our own presence back).
  const refreshState = () => bridge.get_room_state().then(s => {
    setState(s)
    if (s.connected && s.user) setPresence(prev => (prev.includes(s.user) ? prev : [s.user, ...prev]))
  })
  useEffect(() => { refreshState() }, [])
  // Re-check connection whenever the tab becomes active (a chat trigger may have
  // been enabled/disabled while we were elsewhere).
  useEffect(() => { if (active) refreshState() }, [active])

  // Subscribe to the room event stream. Stays mounted across tab switches (App
  // toggles display), so the transcript keeps filling even when not visible.
  useEffect(() => {
    const inRoom = (room: string) => {
      const cur = stateRef.current.room
      return !cur || !room || room === cur
    }
    const onMessage = (e: Event) => {
      const m = (e as CustomEvent<RoomMessage>).detail
      if (!inRoom(m.room)) return
      setMessages(prev => {
        if (m.id && prev.some(x => x.id === m.id)) return prev   // dedupe by id
        return [...prev, { sender: m.sender, text: m.text, id: m.id, ts: m.ts }]
      })
    }
    const onPresence = (e: Event) => {
      const p = (e as CustomEvent<RoomPresence>).detail
      if (!inRoom(p.room)) return
      setPresence(prev => {
        const without = prev.filter(u => u !== p.user)
        return p.status === 'available' ? [...without, p.user] : without
      })
    }
    const onDraft = (e: Event) => {
      const d = (e as CustomEvent<{ id: string; room: string; text: string }>).detail
      if (!inRoom(d.room)) return
      setDraft(d.text)
      message.info('The agent proposed a reply — review and send it.')
      inputRef.current?.focus?.()
    }
    // The reconnect supervisor pushes live connection state (connect/drop), so
    // the header reflects an always-on connection coming and going.
    const onConnection = (e: Event) => {
      const c = (e as CustomEvent<{ connected: boolean }>).detail
      setState(prev => ({ ...prev, connected: !!c.connected }))
      if (!c.connected) setPresence(prev => prev.filter(u => u === stateRef.current.user))
    }
    window.addEventListener('runspec:room_message', onMessage)
    window.addEventListener('runspec:room_presence', onPresence)
    window.addEventListener('runspec:room_draft', onDraft)
    window.addEventListener('runspec:room_connection', onConnection)
    return () => {
      window.removeEventListener('runspec:room_message', onMessage)
      window.removeEventListener('runspec:room_presence', onPresence)
      window.removeEventListener('runspec:room_draft', onDraft)
      window.removeEventListener('runspec:room_connection', onConnection)
    }
  }, [])

  // Keep the transcript pinned to the newest message.
  useEffect(() => {
    const el = transcriptRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  const send = async () => {
    const text = draft.trim()
    if (!text || sending) return
    setSending(true)
    try {
      const res = await bridge.send_room_message(stateRef.current.room, text)
      if (!res.ok) { message.error(res.error || 'Could not send the message'); return }
      // The server drops our own echo, so render it ourselves.
      setMessages(prev => [...prev, {
        sender: stateRef.current.user || 'me', text, id: 'local-' + Date.now(),
        ts: Date.now() / 1000, mine: true,
      }])
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

  // Not configured at all → guide the user instead of showing an empty shell.
  if (!state.room) {
    return (
      <div style={{ padding: 48, display: 'flex', justifyContent: 'center' }}>
        <Empty
          description={
            <span>
              No chat room configured.<br />
              Set <Text code>[chat]</Text> <Text code>host</Text> / <Text code>port</Text> / <Text code>room</Text> in
              your config to join — the console connects automatically.
            </span>
          }
        />
      </div>
    )
  }

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      {/* Header: room + connection + identity */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10, padding: '10px 16px',
        borderBottom: `1px solid ${border}`, flexShrink: 0,
      }}>
        <Text strong style={{ fontSize: 14 }}>#{state.room}</Text>
        <span style={{ flex: 1 }} />
        <Tag
          icon={<WifiOutlined />}
          color={state.connected ? 'green' : 'default'}
        >
          {state.connected ? 'connected' : 'offline — reconnecting…'}
        </Tag>
        <Tooltip title="Your display name in the room">
          <Text type="secondary" style={{ fontSize: 12 }}>{state.user}</Text>
        </Tooltip>
      </div>

      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        {/* Transcript */}
        <div ref={transcriptRef} style={{ flex: 1, overflowY: 'auto', padding: '12px 16px' }}>
          {messages.length === 0 ? (
            <div style={{ marginTop: 40 }}>
              <Empty description="No messages yet" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            </div>
          ) : messages.map((m, i) => (
            <div key={m.id || i} style={{ display: 'flex', gap: 8, padding: '3px 0', alignItems: 'baseline' }}>
              <Text strong style={{ color: m.mine ? '#3fb950' : accent, whiteSpace: 'nowrap' }}>{m.sender}</Text>
              <Text style={{ flex: 1, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{m.text}</Text>
              <Text type="secondary" style={{ fontSize: 11, whiteSpace: 'nowrap' }}>{fmtTime(m.ts)}</Text>
            </div>
          ))}
        </div>

        {/* Presence */}
        <div style={{
          width: 170, flexShrink: 0, borderLeft: `1px solid ${border}`,
          background: panelBg, padding: '12px 14px', overflowY: 'auto',
        }}>
          <Text type="secondary" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
            Present · {presence.length}
          </Text>
          <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {presence.length === 0
              ? <Text type="secondary" style={{ fontSize: 12 }}>—</Text>
              : [...presence].sort().map(u => (
                  <div key={u} style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span style={{ color: '#3fb950', fontSize: 10 }}>●</span>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {u}{u === state.user ? ' (you)' : ''}
                    </span>
                  </div>
                ))}
          </div>
        </div>
      </div>

      {/* Composer */}
      <div style={{ display: 'flex', gap: 8, padding: '12px 16px', borderTop: `1px solid ${border}`, flexShrink: 0 }}>
        <Input.TextArea
          ref={inputRef}
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onPressEnter={e => { if (!e.shiftKey) { e.preventDefault(); send() } }}
          placeholder={state.connected ? 'Message the room… (Enter to send, Shift+Enter for a newline)' : 'Offline — reconnecting to the room…'}
          autoSize={{ minRows: 1, maxRows: 4 }}
          disabled={!state.connected}
        />
        <Button
          type="primary"
          icon={<SendOutlined />}
          onClick={send}
          loading={sending}
          disabled={!state.connected || !draft.trim()}
        >
          Send
        </Button>
      </div>
      {/* A faint hint that a pre-filled draft came from the agent. */}
      {draft && (
        <div style={{ padding: '0 16px 8px', marginTop: -4 }}>
          <Text type="secondary" style={{ fontSize: 11 }}>
            <RobotOutlined /> Tip: the agent can propose replies here — you always review and send.
          </Text>
        </div>
      )}
    </div>
  )
}

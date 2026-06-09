import { useEffect, useMemo, useRef, useState } from 'react'
import { Table, Tag, Switch, Alert, Button, Tooltip, Empty, Space } from 'antd'
import { CheckOutlined, CloseOutlined, ReloadOutlined } from '@ant-design/icons'
import { bridge, type TriggerCard, type TriggerListResult } from '../bridge'
import { useIsDark } from '../ThemeContext'

// Board columns, left → right. The engine drives triggered→…→done/failed; the
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

interface PendingConfirm {
  request_id: string
  tool_name: string
  tool_input: Record<string, unknown>
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
  const [notes, setNotes] = useState<string[]>([])
  // Streamed agent text per card id (its reasoning/reply as it arrives).
  const [streams, setStreams] = useState<Record<string, string>>({})
  // Known card ids, so the global runspec:token listener ignores chat tokens.
  const cardIds = useRef<Set<string>>(new Set())

  const loadConfig = () => bridge.list_triggers().then(setConfig).catch(console.error)

  useEffect(() => {
    loadConfig()
    bridge.get_trigger_board().then(board => {
      board.forEach(c => cardIds.current.add(c.card_id))
      setCards(Object.fromEntries(board.map(c => [c.card_id, c])))
    }).catch(console.error)
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
      const { id, request_id, tool_name, tool_input } = (e as CustomEvent).detail as {
        id: string; request_id: string; tool_name: string; tool_input: Record<string, unknown>
      }
      // Only trigger-card confirms (chat confirms use chat ids we don't track).
      setCards(prev => {
        if (prev[id]) {
          setPending(p => ({ ...p, [id]: { request_id, tool_name, tool_input: tool_input ?? {} } }))
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
    const onNote = (e: Event) => {
      const { text } = (e as CustomEvent).detail as { text: string }
      if (text) setNotes(prev => [text, ...prev].slice(0, 20))
    }
    window.addEventListener('runspec:trigger_card', onCard)
    window.addEventListener('runspec:token', onToken)
    window.addEventListener('runspec:tool_confirm', onConfirm)
    window.addEventListener('runspec:run_end', onEnd)
    window.addEventListener('runspec:trigger_status', onStatus)
    window.addEventListener('runspec:trigger_note', onNote)
    return () => {
      window.removeEventListener('runspec:trigger_card', onCard)
      window.removeEventListener('runspec:token', onToken)
      window.removeEventListener('runspec:tool_confirm', onConfirm)
      window.removeEventListener('runspec:run_end', onEnd)
      window.removeEventListener('runspec:trigger_status', onStatus)
      window.removeEventListener('runspec:trigger_note', onNote)
    }
  }, [])

  const answer = (cardId: string, approved: boolean) => {
    const p = pending[cardId]
    if (!p) return
    bridge.resolve_tool_confirmation(p.request_id, approved).catch(console.error)
    setPending(prev => { const next = { ...prev }; delete next[cardId]; return next })
  }

  const toggleEnabled = async (name: string, enabled: boolean) => {
    await bridge.set_trigger_enabled(name, enabled)
    loadConfig()
  }

  // Bucket cards into columns; a pending confirm overrides to awaiting_human.
  const byColumn = useMemo(() => {
    const buckets: Record<string, TriggerCard[]> = {}
    for (const col of COLUMNS) buckets[col.key] = []
    for (const card of Object.values(cards)) {
      const col = pending[card.card_id] ? 'awaiting_human' : card.column
      ;(buckets[col] ?? buckets.triggered).push(card)
    }
    for (const col of COLUMNS) {
      buckets[col.key].sort((a, b) => b.created_at - a.created_at)
    }
    return buckets
  }, [cards, pending])

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
    <div>
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

      {/* Configured triggers */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ color: dim, fontSize: 12 }}>
          {triggers.length} trigger{triggers.length === 1 ? '' : 's'} configured
          {config?.running ? ' · engine running' : ''}
        </span>
        <Tooltip title="Reload trigger config">
          <Button size="small" type="text" icon={<ReloadOutlined />} onClick={loadConfig} style={{ color: dim }} />
        </Tooltip>
      </div>

      {triggers.length === 0 ? (
        <Empty
          description={
            <span style={{ color: dim }}>
              No triggers configured. Add <code>[[triggers]]</code> tables to{' '}
              <code>runspec_triggers.toml</code> in the app config directory.
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
          style={{ marginBottom: 20 }}
        />
      )}

      {/* Kanban board */}
      <div style={{ display: 'flex', gap: 10, overflowX: 'auto', paddingBottom: 8 }}>
        {COLUMNS.map(col => (
          <div
            key={col.key}
            style={{
              flex: '1 0 200px', minWidth: 200, background: colBg,
              border: `1px solid ${border}`, borderRadius: 6, padding: 8,
            }}
          >
            <div style={{
              display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8,
              fontSize: 12, fontWeight: 600, color: COLUMN_COLOR[col.key],
            }}>
              <span style={{ width: 8, height: 8, borderRadius: 8, background: COLUMN_COLOR[col.key] }} />
              {col.label}
              <span style={{ color: dim, fontWeight: 400 }}>{byColumn[col.key].length}</span>
            </div>

            {byColumn[col.key].map(card => {
              const p = pending[card.card_id]
              const result = card.detail?.result as string | undefined
              const error = card.detail?.error as string | undefined
              // Final result once Done; otherwise the live streamed agent text.
              const body = result || streams[card.card_id]
              const pendingPreview = p ? describeToolInput(p.tool_name, p.tool_input) : ''
              return (
                <div
                  key={card.card_id}
                  style={{
                    background: cardBg, border: `1px solid ${border}`, borderRadius: 6,
                    padding: 8, marginBottom: 8, fontSize: 12,
                  }}
                >
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>{card.summary}</div>
                  <div style={{ color: dim, marginBottom: 4 }}>
                    {card.trigger} · {card.source}:{card.event_type}
                  </div>
                  {error && <div style={{ color: COLUMN_COLOR.failed }}>{error}</div>}
                  {body && (
                    <div style={{ color: dim, whiteSpace: 'pre-wrap', maxHeight: 200, overflowY: 'auto' }}>
                      {body}
                    </div>
                  )}
                  {p && (
                    <div style={{ marginTop: 6 }}>
                      <div style={{ color: COLUMN_COLOR.awaiting_human, marginBottom: 4 }}>
                        Approve <code>{p.tool_name}</code>?
                      </div>
                      {pendingPreview && (
                        <div style={{
                          whiteSpace: 'pre-wrap', maxHeight: 180, overflowY: 'auto',
                          background: colBg, border: `1px solid ${border}`, borderRadius: 4,
                          padding: 6, marginBottom: 6,
                        }}>
                          {pendingPreview}
                        </div>
                      )}
                      <Space size={6}>
                        <Button size="small" type="primary" icon={<CheckOutlined />} onClick={() => answer(card.card_id, true)}>Approve</Button>
                        <Button size="small" danger icon={<CloseOutlined />} onClick={() => answer(card.card_id, false)}>Deny</Button>
                      </Space>
                    </div>
                  )}
                  <div style={{ color: dim, marginTop: 4, fontSize: 11 }}>{fmtTime(card.created_at)}</div>
                </div>
              )
            })}
          </div>
        ))}
      </div>

      {notes.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div style={{ color: dim, fontSize: 12, marginBottom: 6 }}>Recent activity</div>
          {notes.map((n, i) => (
            <div key={i} style={{ fontSize: 12, color: dim, fontFamily: 'monospace', marginBottom: 2 }}>{n}</div>
          ))}
        </div>
      )}
    </div>
  )
}
